import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_public
from check_public import check_notebook
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "v2" / "common"))
from redaction import redact_sensitive_data, redact_sensitive_text, register_sensitive_value


class PublicationGuardTests(unittest.TestCase):
    def write_notebook(self, folder, settings, outputs=None):
        path = Path(folder) / "V2_4_Delete_Fabric_Items.ipynb"
        nb = {"cells": [{"cell_type": "code", "source": [settings],
                         "metadata": {"tags": ["parameters"]}, "execution_count": None, "outputs": outputs or []}]}
        path.write_text(json.dumps(nb), encoding="utf-8")
        return path

    def test_real_run_default_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_notebook(folder, 'DRY_RUN = False\nCONFIRM_DELETE_ID = ""\n')
            with self.assertRaisesRegex(ValueError, "DRY_RUN"):
                check_notebook(path)

    def test_confirmation_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_notebook(folder, 'DRY_RUN = True\nCONFIRM_DELETE_ID = "example-confirmation"\n')
            with self.assertRaisesRegex(ValueError, "confirmation"):
                check_notebook(path)

    def test_saved_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_notebook(folder, "DRY_RUN = True\n", [{"output_type": "stream", "text": ["run output"]}])
            with self.assertRaisesRegex(ValueError, "saved output"):
                check_notebook(path)

    def test_diagnostic_redaction_is_structured_and_nonmutating(self):
        secret = "synthetic-" + "credential-canary"
        signature = "synthetic-" + "signature-canary"
        source = {"Status": "Failed", "Rows": 7, "client_secret": secret, "token": secret,
                  "Detail": "https://example.invalid/data?sv=1&sig=" + signature}
        result = redact_sensitive_data(source)
        self.assertTrue(secret not in json.dumps(result) and signature not in json.dumps(result))
        self.assertEqual((result["Status"], result["Rows"]), ("Failed", 7))
        self.assertEqual(source["client_secret"], secret)

    def test_registered_opaque_secret_and_encoded_value_are_hidden(self):
        secret = "synthetic-" + "credential+canary"
        register_sensitive_value(secret)
        text = secret + " synthetic-credential%2Bcanary"
        self.assertNotIn(secret, redact_sensitive_text(text))
        self.assertNotIn("synthetic-credential%2Bcanary", redact_sensitive_text(text))

    def test_credential_patterns_keep_full_diagnostics(self):
        token = "ghp_" + "A" * 36
        fine_grained = "github_pat_" + "A" * 60
        diagnostic = "x" * 2000 + " fix retained"
        text = redact_sensitive_text("Authorization: Bearer " + token + "\n" + fine_grained + "\n" + diagnostic)
        self.assertNotIn(token, text)
        self.assertNotIn(fine_grained, text)
        self.assertIn(diagnostic, text)
        self.assertEqual(redact_sensitive_text(text), text)

    def test_fine_grained_token_blocks_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "sample.py"
            path.write_text('TOKEN = "' + "github_pat_" + "A" * 60 + '"', encoding="utf-8")
            with patch.object(check_public, "ROOT", root), patch.object(check_public, "publication_files", return_value=[path]):
                with self.assertRaisesRegex(ValueError, "Credential-shaped"):
                    check_public.main()

    def test_private_identifier_variants_block_publication(self):
        label = "private-org"
        variants = (label, label.upper(), label.title(), label + "-v2", label + "_seed",
                    "t-" + label, "admin@" + label + ".example")
        for value in variants:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                path = root / "sample.py"
                path.write_text(repr(value), encoding="utf-8")
                with patch.object(check_public, "ROOT", root), patch.object(check_public, "publication_files", return_value=[path]):
                    with self.assertRaisesRegex(ValueError, "Private identifier"):
                        check_public.main(private_markers=[label])

    def test_private_identifier_in_filename_blocks_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "private-org_notes.md"
            path.write_text("Generic migration notes.", encoding="utf-8")
            with patch.object(check_public, "ROOT", root), patch.object(check_public, "publication_files", return_value=[path]):
                with self.assertRaisesRegex(ValueError, "Private identifier"):
                    check_public.main(private_markers=["private-org"])

    def test_fabric_product_name_is_not_customer_branding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "fabric-migration.md"
            path.write_text("Microsoft Fabric migration toolkit.", encoding="utf-8")
            with patch.object(check_public, "ROOT", root), patch.object(check_public, "publication_files", return_value=[path]):
                with self.assertRaisesRegex(ValueError, "Expected eight published notebooks"):
                    check_public.main(private_markers=["private-org"])

    def test_invalid_private_marker_lists_are_rejected(self):
        for markers in ("private-org", [""], [False], None):
            with self.subTest(markers=markers):
                with self.assertRaisesRegex(ValueError, "nonempty strings"):
                    check_public.main(private_markers=markers)


if __name__ == "__main__":
    unittest.main()
