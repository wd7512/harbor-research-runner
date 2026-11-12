def ps_command() -> list[str]:
    return ["ps", "-ww", "-axo", "pid=,pgid=,command="]
