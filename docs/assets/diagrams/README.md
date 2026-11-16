# Diagrams

Figures for these documents are committed PNGs, reproducible from source. New
figures use no Mermaid and no ASCII art.

1. Draw the figure as data in a script in `scripts/diagrams/`, using
   `diagram_kit.py`. `attestation_diagrams.py` is a worked example. Boxes sit on a
   grid and arrows join them. Label every arrow and keep sentences out of the
   drawing.
2. Render it: `python scripts/diagrams/attestation_diagrams.py`. You need Chrome
   and Pillow. A layout fault (clipped text, an overlap, crossing lines) stops the
   run and names the fault.
3. Read the image yourself. The checks catch geometry, not sense.
4. Reference the PNG with alt text that carries the same facts, and a one-line
   caption naming the generator.

The look is one language: IBM Plex on a 960 px canvas with an opaque panel, so a
figure reads on a light or a dark page. Blue, from `#193d9a` to `#98c1ff`, carries
structure and flow. Colour never carries a status alone.

Older figures here keep their `.mmd` sources. Do not add more.
