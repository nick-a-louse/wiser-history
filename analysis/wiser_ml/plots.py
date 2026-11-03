"""Chart helpers for the notebooks: one quiet style, colours by meaning.

Colours are the first two slots of the shared categorical palette (validated
for colour-vision deficiency as a pair), always in the same order: blue for
last winter, orange for this autumn. Text stays in ink colours, never the
series colour.
"""
import matplotlib.pyplot as plt

SURFACE = "#fcfcfb"
INK = "#1f1f1e"
MUTED = "#6b6a63"
GRID = "#e6e5df"
BLUE = "#2a78d6"     # slot 1
ORANGE = "#eb6834"   # slot 2


def style():
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": MUTED, "axes.titlecolor": INK,
        "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left",
        "axes.labelsize": 10, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
        "axes.spines.top": False, "axes.spines.right": False,
        "xtick.color": MUTED, "ytick.color": MUTED, "legend.frameon": False,
        "legend.labelcolor": INK, "lines.linewidth": 2, "figure.figsize": (9, 4.8),
        "figure.dpi": 110,
    })


def season_of(index):
    """'Winter 2025/26' before June 2026, else 'Autumn 2026'."""
    return ["Winter 2025/26" if d.year == 2025 or d.month < 6 else "Autumn 2026" for d in index]


SEASON_COLOURS = {"Winter 2025/26": BLUE, "Autumn 2026": ORANGE}
