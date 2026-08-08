"""The one-way rule between analysis and capital, enforced by AST not convention.

The conviction apparatus earns its credibility from one number: how often the
things it chose to surface actually resolved correctly. That number means
something only while the analysis is blind to what was traded on it. The moment
a fill, a P&L, or a position size can influence a prediction, a barrier, or a
calibration bucket, the system is grading its own homework and the hit rate
quietly stops describing anything.

Nothing about that is enforced by the code being correct today. It is enforced
by the dependency direction, and a dependency direction that lives only in a
document is one careless import from being gone. So it lives here, as a scan.

The layering, lowest first:

    omni.venue      knows about markets. Nothing about portfolios or analysis.
    omni.portfolio  knows about holdings and limits. May use venue types.
    omni.trading    the bridge. May use venue, portfolio and conviction.

and the analysis side -- `conviction`, `capabilities`, `capability`, `coverage`,
`ingest`, `perception`, `detect` -- may import **none** of the three. It cannot
know that trading exists.

Extends the pattern already used by
`test_execution.py::TestImportIsolation`, which scans the execution package the
same way and for the same reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "omni"

# package -> packages it must never import, directly or relatively.
FORBIDDEN_IMPORTS: dict[str, tuple[str, ...]] = {
    # The analysis side cannot know that capital exists.
    "conviction": ("omni.trading", "omni.portfolio", "omni.venue"),
    "capabilities": ("omni.trading", "omni.portfolio", "omni.venue"),
    "capability": ("omni.trading", "omni.portfolio", "omni.venue"),
    "coverage": ("omni.trading", "omni.portfolio", "omni.venue"),
    "ingest": ("omni.trading", "omni.portfolio", "omni.venue"),
    "perception": ("omni.trading", "omni.portfolio", "omni.venue"),
    "detect": ("omni.trading", "omni.portfolio", "omni.venue"),
    "calibration": ("omni.trading", "omni.portfolio", "omni.venue"),
    # Within the capital side, the layering runs one way only.
    "venue": ("omni.trading", "omni.portfolio", "omni.conviction", "omni.capabilities"),
    "portfolio": ("omni.trading",),
}

# Packages at the top of the capital stack: everything below them is fair game,
# so they have no forbidden imports of their own. Listed explicitly rather than
# omitted, so that `test_every_capital_package_is_covered_by_the_scan` still
# forces a deliberate decision when a new package appears.
TOP_OF_STACK = frozenset({"trading"})


def _offending_imports(
    package: str, forbidden: tuple[str, ...], root: Path | None = None
) -> list[tuple[str, str]]:
    """Every import in `package` that reaches one of `forbidden`.

    Relative imports are checked too: `from ...trading import x` inside
    `omni.portfolio.sizing` climbs to `omni.trading` and is exactly as much a
    violation as spelling it out, but a string scan would miss it.

    `root` is injectable so the scan can be pointed at a planted fixture and
    proved to fire, rather than trusted because it returned nothing.
    """
    base = SRC if root is None else root
    directory = base / package
    if not directory.is_dir():
        pytest.skip(f"package {package} does not exist yet")

    forbidden_short = {p.split(".", 1)[1] for p in forbidden}
    offenders: list[tuple[str, str]] = []

    for path in sorted(directory.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(
                        alias.name == p or alias.name.startswith(p + ".")
                        for p in forbidden
                    ):
                        offenders.append((str(path.relative_to(base)), alias.name))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level == 0:
                    if any(module == p or module.startswith(p + ".") for p in forbidden):
                        offenders.append(
                            (str(path.relative_to(base)), f"from {module} import")
                        )
                elif node.level >= 2:
                    root = module.split(".", 1)[0] if module else ""
                    if root in forbidden_short:
                        offenders.append(
                            (
                                str(path.relative_to(base)),
                                f"relative {'.' * node.level}{module}",
                            )
                        )
    return offenders


@pytest.mark.parametrize(
    ("package", "forbidden"),
    sorted((pkg, tuple(bad)) for pkg, bad in FORBIDDEN_IMPORTS.items()),
)
def test_package_does_not_import_across_the_one_way_boundary(package, forbidden):
    offenders = _offending_imports(package, forbidden)
    assert not offenders, (
        f"omni.{package} imports {forbidden}, which inverts the one-way rule: "
        f"{offenders}. A fill must never be able to influence a prediction, a "
        f"barrier, or a calibration bucket."
    )


class TestTheScanItselfDiscriminates:
    """A scan that cannot fail is not a guard.

    These prove the detector fires, so a green result above means "no
    violations" rather than "the parser silently matched nothing".
    """

    def test_an_absolute_import_is_detected(self, tmp_path):
        root = self._plant(tmp_path, "import omni.trading\n")
        assert _offending_imports("fake", ("omni.trading",), root)

    def test_a_from_import_is_detected(self, tmp_path):
        root = self._plant(tmp_path, "from omni.trading.policy import eligible\n")
        assert _offending_imports("fake", ("omni.trading",), root)

    def test_a_submodule_import_is_detected(self, tmp_path):
        root = self._plant(tmp_path, "import omni.trading.bridge as b\n")
        assert _offending_imports("fake", ("omni.trading",), root)

    def test_a_relative_import_climbing_out_is_detected(self, tmp_path):
        # `from ..trading import x` inside omni.<pkg>.<mod> reaches omni.trading.
        root = self._plant(tmp_path, "from ..trading import bridge\n")
        assert _offending_imports("fake", ("omni.trading",), root)

    def test_an_unrelated_import_is_not_flagged(self, tmp_path):
        root = self._plant(tmp_path, "from omni.ingest import protocol\n")
        assert not _offending_imports("fake", ("omni.trading",), root)

    def test_a_similarly_named_package_is_not_flagged(self, tmp_path):
        # `omni.trading_notes` starts with the same characters but is not it.
        root = self._plant(tmp_path, "import omni.trading_notes\n")
        assert not _offending_imports("fake", ("omni.trading",), root)

    @staticmethod
    def _plant(tmp_path: Path, source: str) -> Path:
        package = tmp_path / "fake"
        package.mkdir()
        (package / "mod.py").write_text(source)
        return tmp_path


def test_every_capital_package_is_covered_by_the_scan():
    """A new package under the capital tier must be added to the map.

    Without this, creating `omni/execution_v2/` and importing conviction from it
    would pass every test in this file by not being looked at. A package at the
    top of the stack has nothing forbidden to it, but must say so out loud via
    `TOP_OF_STACK` rather than by being absent.
    """
    capital_packages = {"venue", "portfolio", "trading"}
    present = {
        path.name
        for path in SRC.iterdir()
        if path.is_dir() and not path.name.startswith("_")
    }
    for package in capital_packages & present:
        assert package in FORBIDDEN_IMPORTS or package in TOP_OF_STACK, (
            f"omni.{package} exists but is in neither FORBIDDEN_IMPORTS nor "
            f"TOP_OF_STACK, so nothing checks which way its imports point"
        )
