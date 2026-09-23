"""M2.16 proof: provider seams with deterministic, dependency-free defaults, and engine blindness.

ALPACA-NATIVE: the upstream Rune-2 tree ships NO `tests/test_providers.py`. The seam
(rune2/providers/{base,deterministic,registry}.py) is exercised upstream only through the full
answer door and the determinism machine. This module is therefore written directly to the vendored
seam rather than ported, and borrows its fixtures and shape from the three nearest upstream tests:

  * tests/test_r4_determinism.py    - byte-reproducibility of a deterministic surface across two
                                      runs (test_answer_determinism_hash_stable); we reuse the
                                      "same input -> byte-identical output, different input ->
                                      different output" teeth against the HashEmbedder.
  * tests/test_pkt2_det.py          - env-invariance across a PYTHONHASHSEED / locale change proved
                                      by two independent subprocesses (TestCanonicalSurfaceDm2.
                                      _digest); we reuse the subprocess pattern to prove the
                                      deterministic embedder is seed- and locale-invariant.
  * tests/test_narrow_profile.py    - the seam-selection defaults live in config/meta and are the
                                      off-by-default stand-ins; we reuse the "assert the shipped
                                      default is the deterministic stand-in, and the opt-in is
                                      explicit" shape for the four provider keys.

Every property is asserted on BOTH a positive and a negative path. The four Done-when facts are
covered:
  * the deterministic providers resolve with no network and no model dependency (the four defaults
    are deterministic-hash-64 / identity / structural-only / off, shipped unchanged);
  * their output is reproducible across runs and invariant to PYTHONHASHSEED and locale;
  * provider selection is read from project.yaml, mapping one-to-one onto the four rune.toml keys;
  * the engine is BLIND to the seam: no module under alpaca/wiki/engine imports a provider module, and
    a substituted provider is invisible at the consuming surface (fuse consumes the .rerank duck,
    never a concrete import).
"""
import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11 has no stdlib tomllib
    tomllib = None

import alpaca
from alpaca.wiki.config import Config
from alpaca.wiki.providers import base
from alpaca.wiki.providers.deterministic import (
    HashEmbedder,
    IdentityReranker,
    NullLLMExtractor,
    StructuralEntailer,
)
from alpaca.wiki.providers.registry import (
    Providers,
    get_embedder,
    get_entailer,
    get_llm_extractor,
    get_reranker,
)
from alpaca.wiki.engine import fuse
from alpaca.wiki.types import RetrievalHit

# The project root is the parent of the `alpaca` package; project.yaml and alpaca/wiki/rune.toml.example
# are located from it without ever naming an absolute harness path (rename-safe).
ROOT = Path(alpaca.__file__).resolve().parent.parent
ENGINE_DIR = Path(alpaca.__file__).resolve().parent / "wiki" / "engine"
PROJECT_YAML = ROOT / "project.yaml"
RUNE_TOML_EXAMPLE = Path(alpaca.__file__).resolve().parent / "wiki" / "rune.toml.example"

# The four seam keys shared by rune.toml and project.yaml.wiki_providers.
SEAM_KEYS = ("embedder", "reranker", "entailer", "llm_extractor")


def _cfg(**over):
    """A throwaway Config with seam selections overridden (no store is poured)."""
    v = Path(tempfile.mkdtemp(prefix="tewiki_m216_"))
    return Config(
        vault_dir=v,
        db_path=v / "rune.db",
        ledger_path=v / "ledger" / "events.jsonl",
        projection_dir=v / "projection",
        **over,
    )


# ============================================================ the four deterministic defaults
class TestDeterministicDefaults(unittest.TestCase):
    """The shipped defaults are the deterministic, dependency-free stand-ins - never models."""

    def test_registry_resolves_the_four_documented_defaults(self):
        # POSITIVE: the default Config selects the exact four upstream stand-ins, by name.
        p = Providers(_cfg())
        self.assertEqual(p.embedder.name, "deterministic-hash-64")
        self.assertEqual(p.reranker.name, "identity")
        self.assertEqual(p.entailer.name, "structural-only")
        self.assertEqual(p.llm_extractor.name, "off")
        # the resolved instances are the deterministic classes, not some real model.
        self.assertIsInstance(p.embedder, HashEmbedder)
        self.assertIsInstance(p.reranker, IdentityReranker)
        self.assertIsInstance(p.entailer, StructuralEntailer)
        self.assertIsInstance(p.llm_extractor, NullLLMExtractor)

    def test_unknown_selection_is_refused_not_silently_defaulted(self):
        # NEGATIVE: an unregistered name raises rather than falling back to a model or to silence.
        for getter, over in (
            (get_embedder, {"embedder": "gpt-embed-3"}),
            (get_reranker, {"reranker": "mxbai-rerank-v2"}),
            (get_entailer, {"entailer": "hhem-ensemble"}),
            (get_llm_extractor, {"llm_extractor": "claude"}),
        ):
            with self.assertRaises(ValueError):
                getter(_cfg(**over))

    def test_llm_extractor_is_off_and_yields_nothing(self):
        # The plan ships the LLM extractor OFF: the seam exists but produces no edges.
        ext = get_llm_extractor(_cfg())
        self.assertEqual(ext.name, "off")
        self.assertEqual(ext.extract("any block text", "any context"), [])
        # POSITIVE: the off-synonyms all resolve to the null extractor; a real name does not.
        for name in ("off", "none", ""):
            self.assertIsInstance(get_llm_extractor(_cfg(llm_extractor=name)), NullLLMExtractor)


# ============================================================ reproducible + env-invariant output
class TestReproducibleEmbedding(unittest.TestCase):
    """The deterministic embedder is byte-reproducible across runs and invariant to env
    (borrowed shape: test_r4_determinism.test_answer_determinism_hash_stable + test_pkt2_det)."""

    def test_same_input_is_byte_identical_and_different_input_differs(self):
        emb = HashEmbedder()
        text = "[[Ada]] works at [[Acme]]."
        v1 = emb.embed(text)
        v2 = emb.embed(text)
        # POSITIVE: reproducible - the same text embeds to byte-identical floats across two calls.
        self.assertEqual(v1, v2)
        # NEGATIVE CONTROL: the embedder is not a constant function - different text differs, so the
        # reproducibility above is a real property and not a trivially-constant output.
        self.assertNotEqual(v1, emb.embed("Grace works at Beta Industries."))

    def test_embedding_is_invariant_across_hashseed_and_locale(self):
        # Faithful env test (test_pkt2_det pattern): two independent interpreters embed the SAME text
        # under DIFFERENT PYTHONHASHSEED values and DIFFERENT locales and MUST emit byte-identical
        # digests. The deterministic embedder walks its tokens in list order and hashes with sha256,
        # so nothing env-dependent can leak into the vector.
        a = self._digest(seed="0", lc="C")
        b = self._digest(seed="524287", lc="C.UTF-8")
        self.assertEqual(a, b, msg="deterministic embedder must be seed/locale invariant")
        # NEGATIVE: the digest is content-derived, so a different text yields a different digest.
        c = self._digest(seed="0", lc="C", text="a completely different sentence entirely")
        self.assertNotEqual(a, c)

    @staticmethod
    def _digest(seed: str, lc: str, text: str = "[[Ada]] works at [[Acme]].") -> str:
        script = (
            "import sys, os, hashlib, json\n"
            "sys.path.insert(0, os.environ['ALPACA_ROOT'])\n"
            "from alpaca.wiki.providers.deterministic import HashEmbedder\n"
            "v = HashEmbedder().embed(os.environ['ALPACA_TEXT'])\n"
            "print(hashlib.sha256(json.dumps(v, sort_keys=True).encode('utf-8')).hexdigest())\n"
        )
        env = dict(os.environ)
        env["ALPACA_ROOT"] = str(ROOT)
        env["ALPACA_TEXT"] = text
        env["PYTHONHASHSEED"] = seed
        env["LC_ALL"] = lc
        out = subprocess.run([sys.executable, "-c", script],
                             capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT))
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()


# ============================================================ selection read from project.yaml
class TestSelectionFromProjectYaml(unittest.TestCase):
    """Step 3: the on-premises-only project runs with no external call BY CONFIGURATION - the four
    keys in project.yaml.wiki_providers map one-to-one onto the four upstream rune.toml keys."""

    def test_project_yaml_and_rune_toml_carry_the_same_four_seam_keys(self):
        doc = yaml.safe_load(PROJECT_YAML.read_text(encoding="utf-8"))
        sel = doc.get("wiki_providers")
        self.assertIsInstance(sel, dict, msg="project.yaml must carry a wiki_providers mapping")
        self.assertEqual(set(sel), set(SEAM_KEYS))
        # the four keys are exactly the four rune.toml seam keys (mapping is one-to-one).
        if tomllib is not None:
            toml = tomllib.loads(RUNE_TOML_EXAMPLE.read_text(encoding="utf-8"))
            for k in SEAM_KEYS:
                self.assertIn(k, toml, msg=f"rune.toml.example is missing seam key {k}")

    def test_selection_from_project_yaml_resolves_the_deterministic_providers(self):
        # POSITIVE: feed the project.yaml selection through the registry exactly as the engine would
        # and get the deterministic, dependency-free stand-ins - no network, no model.
        sel = yaml.safe_load(PROJECT_YAML.read_text(encoding="utf-8"))["wiki_providers"]
        p = Providers(_cfg(**{k: str(sel[k]) for k in SEAM_KEYS}))
        self.assertEqual(p.embedder.name, "deterministic-hash-64")
        self.assertEqual(p.reranker.name, "identity")
        self.assertEqual(p.entailer.name, "structural-only")
        self.assertEqual(p.llm_extractor.name, "off")

    def test_a_selection_naming_an_unregistered_provider_is_refused(self):
        # NEGATIVE: a project.yaml that selected a not-yet-registered model fails closed, so a
        # misconfigured on-prem vault never silently reaches for something that is not present.
        bad = {"embedder": "deterministic", "reranker": "identity",
               "entailer": "structural", "llm_extractor": "gpt-4o-extract"}
        with self.assertRaises(ValueError):
            Providers(_cfg(**bad))


# ============================================================ engine blindness to the seam (Step 4)
def _provider_imports(py: Path) -> list[str]:
    """Every import in `py` that names the providers package (relative, absolute, or dynamic)."""
    hits: list[str] = []
    tree = ast.parse(py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # a relative `from ..providers... import X` (level>0) or an absolute alpaca.wiki.providers.
            if (node.level and mod.split(".")[0] == "providers") or "providers" in mod.split("."):
                hits.append(f"from {'.' * node.level}{mod} import "
                            + ",".join(a.name for a in node.names))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "providers" in alias.name.split("."):
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.Call):
            fn = node.func
            is_imp = ((isinstance(fn, ast.Attribute) and fn.attr == "import_module")
                      or (isinstance(fn, ast.Name) and fn.id == "import_module"))
            if is_imp and node.args and isinstance(node.args[0], ast.Constant):
                val = node.args[0].value
                if isinstance(val, str) and "providers" in val.split("."):
                    hits.append(f"import_module({val!r})")
    return hits


class TestEngineBlindness(unittest.TestCase):
    def test_no_engine_module_imports_a_provider_module_directly(self):
        # POSITIVE (the blindness property): NO module under alpaca/wiki/engine reaches a provider
        # module by any import form. The engine receives providers threaded in as a bundle and only
        # ever touches the seam interface (.embed / .rerank / .entail), so a substituted provider is
        # invisible to it.
        offenders = {}
        for py in sorted(ENGINE_DIR.rglob("*.py")):
            hits = _provider_imports(py)
            if hits:
                offenders[py.name] = hits
        self.assertEqual(offenders, {}, msg=f"engine modules import a provider: {offenders}")

    def test_the_scanner_has_teeth(self):
        # NEGATIVE CONTROL: prove the scanner would CATCH a provider import (P4 - a selftest that
        # cannot fail is not a selftest). A synthetic engine module that imports the registry is
        # flagged; the same module without the import is clean.
        d = Path(tempfile.mkdtemp(prefix="tewiki_m216_scan_"))
        offending = d / "leaky.py"
        offending.write_text("from ..providers.registry import Providers\nx = 1\n", encoding="utf-8")
        self.assertTrue(_provider_imports(offending))
        clean = d / "blind.py"
        clean.write_text("from ..store.db import DB\nx = 1\n", encoding="utf-8")
        self.assertEqual(_provider_imports(clean), [])

    def test_substituted_provider_is_invisible_at_the_consuming_surface(self):
        # The engine's fusion surface (alpaca/wiki/engine/fuse.py) consumes a reranker purely by its
        # .rerank duck. Swap the deterministic IdentityReranker for a differently-named substitute
        # behind the same seam and the engine cannot tell: fuse produces a valid ordering with
        # either, having imported neither.
        hits = [RetrievalHit(block_id="d#a", text="A"), RetrievalHit(block_id="d#b", text="B")]
        channel_lists = {"bm25": ["d#a", "d#b"]}

        class _SubstituteReranker:
            name = "substitute-not-a-real-import"

            def rerank(self, query, blocks):
                return {b: 0.0 for b, _ in blocks}

        real = IdentityReranker()
        sub = _SubstituteReranker()
        # both satisfy the seam protocol - that is all the engine ever relies on.
        self.assertIsInstance(real, base.Reranker)
        self.assertIsInstance(sub, base.Reranker)
        order_real = [h.block_id for h in fuse.fuse(list(hits), channel_lists, real, "q", 60)]
        order_sub = [h.block_id for h in fuse.fuse(list(hits), channel_lists, sub, "q", 60)]
        # POSITIVE: the identity/zero-mass reranker leaves the fusion order intact under either
        # implementation - the substitution is invisible.
        self.assertEqual(order_real, order_sub)
        self.assertEqual(set(order_real), {"d#a", "d#b"})

        # NEGATIVE CONTROL: an object missing the .rerank method is NOT a Reranker at the seam, so
        # the protocol really discriminates (the blindness rests on a real interface, not nothing).
        class _NotAReranker:
            name = "no-rerank-method"

        self.assertNotIsInstance(_NotAReranker(), base.Reranker)


if __name__ == "__main__":
    unittest.main()
