Make the Harbor guard lifecycle test establish child activity before closing the lifeline. This removes its assumption that the child is scheduled during the SIGTERM grace period.
