# webapp.project

Refresh the serving catalog from the gold tier (`axi webapp project`). Fails
closed: a projection that cannot reach its database says so rather than
serving a silently stale catalog. See `skills/project.py`.
