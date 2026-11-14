"""The saved report carries the graph as a self-contained picture, and none without one."""

import re
from pathlib import Path

from lockwire.relevance import robot_graph
from lockwire.report_html import render_html
from lockwire.schema import load_contract

HEX = Path(__file__).parents[1] / "src/lockwire/examples/hexapod-param/contract.yaml"


def test_report_has_the_graph_svg_with_every_node_and_edge_and_stays_self_contained():
    c = load_contract(HEX)
    g = robot_graph(c)
    html = render_html(c)
    svg = html[html.index('<svg id="graphsvg"') : html.index("</svg>", html.index('id="graphsvg"'))]
    assert svg.count('class="gnode ') == len(g["nodes"]) > 10
    assert svg.count('class="gedge ') == len(g["edges"])
    assert not re.search(r"""(?:src|href)=["']?(?:https?:)?//""", svg)
    assert "<image" not in svg and "<script" not in svg


def test_report_graph_places_a_part_right_of_what_feeds_it():
    c = load_contract(HEX)
    html = render_html(c)

    def x(label):
        return int(re.search(rf'<rect x="(\d+)"[^>]*/><text[^>]*>{label}</text>', html).group(1))

    joint = next(n["label"] for n in robot_graph(c)["nodes"] if n["kind"] == "joint")
    link = next(e["to"] for e in robot_graph(c)["edges"] if e["from"] == f"joint:{joint}")
    assert x(joint) < x(link.split(":", 1)[1])


def test_report_writes_no_graph_section_without_a_graph(empty_budget_contract):
    assert robot_graph(empty_budget_contract)["edges"] == []
    html = render_html(empty_budget_contract)
    assert 'id="robotgraph"' not in html and 'id="graphsvg"' not in html
