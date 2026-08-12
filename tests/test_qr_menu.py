"""Tests for QR / Google Drive price-folder parsing."""

from pipeline.qr_menu import folder_id_from_url, parse_embedded_folder


SAMPLE_EMBED = """
<div class="flip-entries">
  <div class="flip-entry" id="entry-1abcFOLDER" tabindex="0" role="link">
    <div class="flip-entry-info">
      <a href="https://drive.google.com/drive/folders/1abcFOLDER">
        <div class="flip-entry-title">MENU</div>
      </a>
    </div>
  </div>
  <div class="flip-entry" id="entry-1fileMeals" tabindex="0" role="link">
    <div class="flip-entry-info">
      <a href="https://drive.google.com/file/d/1fileMeals/view?usp=drive_web">
        <div class="flip-entry-title">Meals and Protein</div>
      </a>
    </div>
  </div>
</div>
"""


def test_folder_id_from_qr_url() -> None:
    assert (
        folder_id_from_url(
            "https://drive.google.com/drive/folders/1XwwTRp6zoqGUuvsFC0wyFGNA1IVCWyoN"
        )
        == "1XwwTRp6zoqGUuvsFC0wyFGNA1IVCWyoN"
    )


def test_parse_embedded_folder_files_and_subfolders() -> None:
    entries = parse_embedded_folder(SAMPLE_EMBED)
    kinds = {e.kind: e.title for e in entries}
    assert kinds["folder"] == "MENU"
    assert kinds["file"] == "Meals and Protein"
    assert entries[1].file_id == "1fileMeals"
