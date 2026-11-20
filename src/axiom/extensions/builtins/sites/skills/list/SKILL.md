---
name: sites.list
description: The sites you may see on this node, with their names, which ones the scope selector has chosen, and whether there is anything to choose. Reads only.
---

# sites.list

Lists the tenant sites the asker may read: the node's tenant registry
(`$AXIOM_TENANTS_FILE`) and `AXIOM_SERVED_SITES`, each kept only when the one
decision function permits the read. A site the asker may not read is not listed
and not counted. `show_selector` is false when there is one site or none, so
people with one site never see the scope selector.
