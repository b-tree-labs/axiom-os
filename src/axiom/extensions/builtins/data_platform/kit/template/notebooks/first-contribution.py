# ---
# jupyter:
#   jupytext:
#     text_representation:
#       extension: .py
#       format_name: percent
#   kernelspec:
#     display_name: Python 3
#     name: python3
# ---

# %% [markdown]
# # Your first contribution
#
# This notebook walks the whole loop on the example data that came with the
# kit, then shows you where to change things to make it yours. Every cell calls
# the same `data kit-*` verbs and the same tools your assistant has, so
# you can switch between them at any point.
#
# Open it with `jupytext --to ipynb data/notebooks/first-contribution.py` (or
# straight from VS Code), and run the cells top to bottom. You need Docker
# running for the local medallion.

# %%
from pathlib import Path

from axiom.extensions.builtins.data_platform import kit

project = kit.load(Path.cwd())
print(f"tenant {project.tenant}; your gold schema is {project.gold_schema}")

# %% [markdown]
# ## 1. A local medallion
#
# The same Postgres schema the shared platform runs, on your machine. Your
# queries run as a role that can only see your site's rows, exactly as they
# will after promotion.

# %%
kit.up(project)

# %% [markdown]
# ## 2. Every tier, on your data
#
# Bronze (the samples in `samples/bronze/`) goes through your normalizer
# (`conform/`), then your silver declarations (`silver/`), then your gold SQL
# (`gold/`); then each verb and chart runs.

# %%
report = kit.try_kit(project)
silver = report["silver"]
print(f"{silver['rows_in']} records in → {silver['rows_out']} silver rows, {silver['errored']} errored")
for line in silver["declarations"]:
    print(" ", line)

# %% [markdown]
# ## 3. Look at what you built

# %%
from IPython.display import SVG, display  # noqa: E402

for chart in report["charts"]:
    display(SVG(filename=chart["svg"]))

for name, rows in report["verbs"].items():
    print(name)
    for row in rows:
        print("  ", row)

# %% [markdown]
# ## 4. Make it yours
#
# Each of these is a small edit, and the next `kit.try_kit(project)` shows the
# effect.
#
# - **Your raw data.** Put a few of your own bronze records in
#   `samples/bronze/<source>/_rows/<day>/part-0.jsonl`, add the source to
#   `kit.toml`, and copy `conform/rig_frame.py` to a normalizer for them.
# - **A building block.** Add a `[[derived]]` to `silver/derived.toml`, e.g. a
#   heat rate from a temperature rise and a flow.
# - **A gold object.** Add `gold/<name>.sql` (one SELECT) and `gold/<name>.toml`
#   with a description. Chat reads the description.
# - **A question.** Add `verbs/<name>.toml`; it becomes a tool in chat for
#   everyone who can read your site.
# - **A chart.** Add `charts/<name>.json` over any gold object.
#
# Your assistant can do any of these with you: ask it to "add a gold object
# for hourly means and try it", and it will edit the files and run the same
# verbs you see here.

# %% [markdown]
# ## 5. The gate
#
# `kit.check_kit` is what CI runs before anything is promoted: your
# declarations, your normalizer against every sample record, and a proof that
# none of your objects can see another site's rows.

# %%
result = kit.check_kit(project)
print("ready for a pull request to your site repository" if result["ok"] else "not yet:")
for key in ("declarations", "normalizers"):
    for finding in result[key]:
        print("  ✗", finding)
print("isolation:", "proven" if result["isolation"] == [] else result["isolation"])

# %% [markdown]
# ## 6. Promote
#
# Commit `data/` and open a pull request on your site repository. CI runs the
# same check. Once it is merged, your gold objects, verbs and charts appear for
# everyone who can read your site. Normalizers are reviewed by the platform
# team before they run on shared data; the guide says why.
