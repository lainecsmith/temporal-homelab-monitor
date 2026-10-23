import socket

CONTROLLER_QUEUE = "controller"


def local_hostname() -> str:
    return socket.gethostname().split(".")[0].lower()


def host_queue(host: str) -> str:
    return f"host-{host}"


def host_workflow_id(host: str) -> str:
    return f"host-{host}"
