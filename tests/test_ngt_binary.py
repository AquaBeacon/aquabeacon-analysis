import struct
import zlib

import pytest

from aquabeacon_analysis.ngt_binary import decode_scal, export_spresense_bin_means


def scal_record():
    raw = bytearray(64)
    raw[:8] = b"SCAL\x01\x01\x00\x00"
    struct.pack_into("<I11f", raw, 8, 3, 9.80665, 38, .01, -.02, .03, 1, 1, 1,
                     .001, -.002, .003)
    struct.pack_into("<I", raw, 60, zlib.crc32(raw[:60]))
    return raw


def test_scal_preserves_coefficients_and_has_no_invented_date():
    result = decode_scal(scal_record())
    assert result["generation"] == 3 and result["enabled"]
    assert result["accel_bias_m_s2"] == pytest.approx([.01, -.02, .03])
    assert result["creation_time"] is None


def test_scal_rejects_corruption_and_invalid_coefficients():
    raw = scal_record()
    raw[20] ^= 1
    with pytest.raises(ValueError, match="CRC"):
        decode_scal(raw)
    struct.pack_into("<f", raw, 32, 1.2)
    struct.pack_into("<I", raw, 60, zlib.crc32(raw[:60]))
    with pytest.raises(ValueError, match="coefficients"):
        decode_scal(raw)


def test_bin_means_preserve_gaps_and_reject_unanchored_rows(tmp_path, monkeypatch):
    import csv
    from types import SimpleNamespace

    fields = ["gx", "gy", "gz", "ax", "ay", "az", "corrected_gx", "corrected_gy",
              "corrected_gz", "corrected_ax", "corrected_ay", "corrected_az"]
    records = []
    for utc, quality, value in [(10001, 2, 1), (10003, 2, 3), (20001, 1, 100),
                                (40001, 2, 7)]:
        records.append({"sample_utc_us": utc, "sample_mono_us": utc - 10000,
                        "time_quality": quality, **dict.fromkeys(fields, value)})
    monkeypatch.setattr("aquabeacon_analysis.ngt_binary.load_reader",
                        lambda *args: SimpleNamespace(iter_rows=lambda source: iter(records)))
    source = tmp_path / "source.bin"
    source.write_bytes(b"fixture")
    target = tmp_path / "means.csv"
    report = export_spresense_bin_means([source], target, tmp_path)
    with target.open() as handle:
        actual = list(csv.DictReader(handle))
    assert report["rejected_no_pps_utc"] == 1
    assert len(actual) == 2
    assert actual[0]["sample_utc_us"] == "10002"
    assert actual[0]["gx"] == "2.0"
    assert actual[0]["count"] == "2"
    assert actual[1]["sample_utc_us"] == "40001"
