Unit tests now reject socket binds, matching the existing socket-connect guard. Run launch-check unit tests use a fake port binder so they do not depend on whether the host proxy port is occupied.
