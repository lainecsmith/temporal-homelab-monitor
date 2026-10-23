"""Update checks that run ON the host: OS packages and container images. Slow and network-bound,
so the workflow runs these on a much longer interval than collect_host_info."""
import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from temporalio import activity

from activities.host_info import _docker_json, _run, _split_image

MAX_PACKAGES = 100  # keeps the snapshot small on hosts that haven't been updated in ages


def _host_result(manager: str, packages: list, **extra) -> dict:
    return {
        "manager": manager,
        "count": len(packages),
        "security_count": None,
        "reboot_required": None,
        "index_age_h": None,
        "packages": packages[:MAX_PACKAGES],
        "error": None,
        **extra,
    }


def _apt() -> dict:
    # Reads the cached package index; doesn't refresh it (that needs root). Most distros refresh it
    # daily via apt-daily.timer, and index_age_h shows how stale it is.
    out = subprocess.run(["apt", "list", "--upgradable"], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return _host_result("apt", [], error=out.stderr.strip() or f"apt exited {out.returncode}")
    packages, security = [], 0
    for line in out.stdout.splitlines():
        # openssl/jammy-updates,jammy-security 3.0.2-0ubuntu1.18 amd64 [upgradable from: 3.0.2-0ubuntu1.15]
        m = re.match(r"^(\S+?)/(\S+) (\S+) \S+ \[upgradable from: ([^\]]+)\]", line)
        if not m:
            continue
        is_security = "-security" in m.group(2)
        security += is_security
        packages.append({
            "name": m.group(1), "current": m.group(4), "available": m.group(3), "security": is_security,
        })
    try:
        index_age_h = round((time.time() - os.path.getmtime("/var/cache/apt/pkgcache.bin")) / 3600, 1)
    except OSError:
        index_age_h = None
    return _host_result(
        "apt", packages,
        security_count=security,
        reboot_required=os.path.exists("/var/run/reboot-required"),
        index_age_h=index_age_h,
    )


def _dnf() -> dict:
    # Exit 100 means updates are available, 0 means none.
    out = subprocess.run(["dnf", "-q", "check-update"], capture_output=True, text=True, timeout=180)
    if out.returncode not in (0, 100):
        return _host_result("dnf", [], error=out.stderr.strip() or f"dnf exited {out.returncode}")
    packages = []
    for line in out.stdout.splitlines():
        if line.startswith("Obsoleting"):
            break
        parts = line.split()
        if len(parts) == 3:  # name.arch  version  repo
            packages.append({"name": parts[0], "current": None, "available": parts[1], "security": None})
    reboot = None
    if shutil.which("needs-restarting"):
        # Exit 1 means a reboot is needed.
        reboot = subprocess.run(["needs-restarting", "-r"], capture_output=True, timeout=60).returncode == 1
    return _host_result("dnf", packages, reboot_required=reboot)


def _pacman() -> dict:
    # checkupdates (pacman-contrib) syncs to a temp db, so it's safe without root. Exit 2 means none.
    out = subprocess.run(["checkupdates"], capture_output=True, text=True, timeout=180)
    if out.returncode not in (0, 2):
        return _host_result("pacman", [], error=out.stderr.strip() or f"checkupdates exited {out.returncode}")
    packages = []
    for line in out.stdout.splitlines():
        m = re.match(r"^(\S+) (\S+) -> (\S+)", line)  # linux 6.10.1-1 -> 6.10.2-1
        if m:
            packages.append({"name": m.group(1), "current": m.group(2), "available": m.group(3), "security": None})
    return _host_result("pacman", packages)


def _softwareupdate() -> dict:
    # Contacts Apple's servers; can take a minute.
    out = subprocess.run(["softwareupdate", "-l"], capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        return _host_result("softwareupdate", [], error=out.stderr.strip() or f"softwareupdate exited {out.returncode}")
    packages, reboot = [], False
    for line in out.stdout.splitlines():
        # * Label: macOS Sequoia 15.6.1-24G90
        # 	Title: macOS Sequoia 15.6.1, Version: 15.6.1, Size: ..., Recommended: YES, Action: restart,
        if line.startswith("* Label: "):
            packages.append({"name": line[len("* Label: "):], "current": None, "available": None, "security": None})
        elif packages and "Title:" in line:
            m = re.search(r"Version: ([^,]+)", line)
            packages[-1]["available"] = m.group(1) if m else None
            reboot |= "Action: restart" in line
    return _host_result("softwareupdate", packages, reboot_required=reboot)


def _host_updates() -> Optional[dict]:
    """None if this host has no package manager we know how to ask."""
    try:
        if shutil.which("apt"):
            return _apt()
        if shutil.which("dnf"):
            return _dnf()
        if shutil.which("checkupdates"):
            return _pacman()
        if platform.system() == "Darwin" and shutil.which("softwareupdate"):
            return _softwareupdate()
    except (OSError, subprocess.TimeoutExpired) as e:
        return _host_result("unknown", [], error=str(e))
    return None


_MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def _registry_repo(image_name: str) -> tuple:
    """'nginx' -> ('registry-1.docker.io', 'library/nginx'); 'ghcr.io/foo/bar' -> ('ghcr.io', 'foo/bar')"""
    first, _, rest = image_name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, rest
    else:
        registry, repo = "docker.io", image_name
    if registry == "docker.io":
        registry = "registry-1.docker.io"
        if "/" not in repo:
            repo = f"library/{repo}"
    return registry, repo


def _anonymous_token(challenge: str, repo: str) -> str:
    """Follows a 'Bearer realm=...,service=...' challenge to get an anonymous pull token."""
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    if "realm" not in params:
        raise RuntimeError(f"unsupported registry auth: {challenge}")
    query = {"service": params.get("service", ""), "scope": params.get("scope", f"repository:{repo}:pull")}
    with urllib.request.urlopen(f"{params['realm']}?{urllib.parse.urlencode(query)}", timeout=10) as resp:
        body = json.load(resp)
    return body.get("token") or body["access_token"]


def _registry_request(registry: str, repo: str, path: str, method: str = "GET", accept: str = "") -> tuple:
    """Makes a registry API call, getting an anonymous token if asked. Returns (headers, body)."""
    url = path if path.startswith("https://") else f"https://{registry}{path}"
    headers = {"Accept": accept} if accept else {}
    for attempt in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method=method, headers=headers), timeout=10) as resp:
                return resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                headers["Authorization"] = f"Bearer {_anonymous_token(e.headers.get('WWW-Authenticate', ''), repo)}"
                continue
            if e.code in (401, 403):
                raise RuntimeError("registry requires login (private image?)")
            if e.code == 404:
                raise RuntimeError("not found in registry")
            raise RuntimeError(f"registry returned HTTP {e.code}")
    raise RuntimeError("registry auth failed")


def _remote_digest(image_name: str, tag: str) -> str:
    """Digest the registry currently serves for name:tag. A HEAD request, so it doesn't count
    against Docker Hub's pull rate limit."""
    registry, repo = _registry_repo(image_name)
    headers, _ = _registry_request(registry, repo, f"/v2/{repo}/manifests/{tag}", "HEAD", _MANIFEST_TYPES)
    digest = headers.get("Docker-Content-Digest")
    if not digest:
        raise RuntimeError("registry didn't return a digest")
    return digest


MAX_TAG_PAGES = 20  # some images have tens of thousands of tags


def _remote_tags(image_name: str) -> list:
    registry, repo = _registry_repo(image_name)
    tags, path = [], f"/v2/{repo}/tags/list?n=1000"
    for _ in range(MAX_TAG_PAGES):
        headers, body = _registry_request(registry, repo, path)
        tags += json.loads(body).get("tags") or []
        # Next page, if any: Link: </v2/library/nginx/tags/list?last=1.27&n=1000>; rel="next"
        m = re.search(r'<([^>]+)>;\s*rel="?next"?', headers.get("Link", ""))
        if not m:
            break
        path = m.group(1)
    return tags


_VERSION_TAG = re.compile(r"^(v?)(\d+(?:\.\d+)*)([-_+].*)?$")


def _parse_version_tag(tag: str) -> Optional[tuple]:
    """'v1.27.2-alpine' -> (('v', 3, '-alpine'), (1, 27, 2)); None for tags like 'latest'.
    The first part is the tag's shape; only tags with the same shape are compared."""
    m = _VERSION_TAG.match(tag)
    if not m:
        return None
    nums = tuple(int(n) for n in m.group(2).split("."))
    return (m.group(1), len(nums), m.group(3) or ""), nums


def _newer_tags(tag: str, available: list) -> dict:
    """Newest tag shaped like the current one, overall and within the same major version.
    Same shape keeps you on the same variant (-alpine stays -alpine) and skips pre-releases (-rc1)."""
    result = {"newest_tag": None, "newest_tag_same_major": None}
    current = _parse_version_tag(tag)
    if not current:
        return result
    shape, current_nums = current
    newer = []
    for t in available:
        parsed = _parse_version_tag(t)
        if parsed and parsed[0] == shape and parsed[1] > current_nums:
            newer.append((parsed[1], t))
    if newer:
        result["newest_tag"] = max(newer)[1]
        # A one-part tag like postgres:16 has no minor versions to stay within.
        same_major = [n for n in newer if len(current_nums) > 1 and n[0][0] == current_nums[0]]
        if same_major:
            result["newest_tag_same_major"] = max(same_major)[1]
    return result


def _local_digests(image: str) -> list:
    out = _run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], timeout=10)
    return [d.partition("@")[2] for d in json.loads(out or "null") or []]


def _image_update(image: str) -> dict:
    name, tag = _split_image(image)
    result = {
        "image": image,
        # Whether the same tag now points at a newer build.
        "status": "unknown", "local_digest": None, "remote_digest": None, "error": None,
        # Whether there's a newer version tag (nginx:1.27 -> 1.28).
        "newest_tag": None, "newest_tag_same_major": None, "tags_error": None,
    }
    if not name or not tag:
        result["error"] = result["tags_error"] = "container isn't running from a tagged image"
        return result

    if _parse_version_tag(tag):
        try:
            result.update(_newer_tags(tag, _remote_tags(name)))
        except (OSError, RuntimeError, ValueError, KeyError) as e:
            result["tags_error"] = str(e)

    local = _local_digests(image)
    if not local:
        result["error"] = "image has no registry digest (built locally?)"
        return result
    result["local_digest"] = local[0]
    try:
        remote = _remote_digest(name, tag)
    except (OSError, RuntimeError, ValueError, KeyError) as e:
        result["error"] = str(e)
        return result
    result["remote_digest"] = remote
    result["status"] = "up_to_date" if remote in local else "update_available"
    return result


def _docker_updates() -> Optional[dict]:
    """Per image: whether its tag now points at a newer build, and whether there's a newer version tag."""
    if not shutil.which("docker"):
        return None
    try:
        ps = _docker_json(["ps", "-a"], timeout=10)
    except RuntimeError as e:
        return {"images": [], "error": str(e)}
    by_image = {}
    for c in ps:
        by_image.setdefault(c["Image"], []).append(c["Names"])
    images = []
    for image, names in by_image.items():
        activity.heartbeat(image)
        images.append({**_image_update(image), "containers": names})
    return {"images": images, "error": None}


@activity.defn
def check_updates() -> dict:
    return {"host": _host_updates(), "docker": _docker_updates()}
