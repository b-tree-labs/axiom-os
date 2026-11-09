# connector.roles

Which connector serves each storage role (`axi connector roles`, ADR-220 §6):
a configured connector, the always-mounted default store (with what that leaves
missing, such as "on this machine only: add an off-site provider"), or absent
(the feature is hidden on this install). Read-only. ADR-056 thin wrapper over
`skills/roles.py:run`.
