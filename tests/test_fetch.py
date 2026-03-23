"""Tests for pipeline.fetch — download media from Synology Photos API."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from pipeline.fetch import FetchConfig, fetch

COLLECT_RESPONSE = {
    "items": [
        {"id": 10, "filename": "IMG_010.jpg", "item_type": 0},
        {"id": 11, "filename": "IMG_011.jpg", "item_type": 0},
    ],
    "count": 2,
    "total_mb": 5.0,
}

LIVE_PHOTO_RESPONSE = {
    "items": [
        {"id": 20, "filename": "LIVE_020.heic", "item_type": 3},
    ],
    "count": 1,
    "total_mb": 3.0,
}


class FakeResponse:
    """Minimal httpx.Response substitute."""

    def __init__(self, data=None, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class FakeStreamResponse:
    """Context-manager returned by client.stream()."""

    def __init__(self, status_code=200, chunks=None):
        self.status_code = status_code
        self._chunks = chunks or [b"fakedata"]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def iter_bytes(self, chunk_size=65536):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeClient:
    """Records calls made through httpx.Client."""

    def __init__(self, collect_response=None, meta_response=None,
                 meta_status=200, stream_response=None):
        self.calls: list[tuple[str, str, dict]] = []
        self._collect = collect_response or COLLECT_RESPONSE
        self._meta = meta_response or {}
        self._meta_status = meta_status
        self._stream = stream_response

    def post(self, url, *, json=None, **kwargs):
        self.calls.append(("POST", url, json or {}))
        return FakeResponse(data=self._collect)

    def get(self, url, *, timeout=None, params=None, **kwargs):
        self.calls.append(("GET", url, params or {}))
        return FakeResponse(data=self._meta, status_code=self._meta_status)

    @contextmanager
    def stream(self, method, url, *, timeout=None, params=None, **kwargs):
        self.calls.append(("STREAM", url, params or {}))
        yield self._stream or FakeStreamResponse()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFetchBuildsCorrectBody:
    def test_fetch_builds_correct_body(self, tmp_path: Path, mock_config):
        """Given all filter params, POST body should contain the right keys."""
        cfg = mock_config
        client = FakeClient()

        with patch("pipeline.fetch._nas.httpx.Client", return_value=client):
            fetch(
                cfg,
                FetchConfig(
                    from_date="2024-01-01",
                    to_date="2024-01-31",
                    country="Singapore",
                    first_level="Central",
                    district="Marina Bay",
                    person_ids=[1, 2],
                    item_types=[0, 1],
                ),
            )

        # Find the POST /api/collect call
        post_calls = [(m, u, b) for m, u, b in client.calls if m == "POST"]
        assert len(post_calls) == 1
        body = post_calls[0][2]
        assert body["from_date"] == "2024-01-01"
        assert body["to_date"] == "2024-01-31"
        assert body["country"] == "Singapore"
        assert body["first_level"] == "Central"
        assert body["district"] == "Marina Bay"
        assert body["person_ids"] == [1, 2]
        assert body["item_types"] == [0, 1]


class TestFetchDownloadsToMediaDir:
    def test_fetch_downloads_to_media_dir(self, tmp_path: Path, mock_config):
        """Files should be written to cfg.media_dir, not workspace/raw."""
        cfg = mock_config
        client = FakeClient()

        with patch("pipeline.fetch._nas.httpx.Client", return_value=client):
            fetch(cfg, FetchConfig())

        # Files land in media_dir
        files = list(cfg.media_dir.iterdir())
        assert len(files) == 2
        # Filenames include item id prefix
        names = sorted(f.name for f in files)
        assert names == ["10_IMG_010.jpg", "11_IMG_011.jpg"]


class TestFetchSkipsCached:
    def test_fetch_skips_cached(self, tmp_path: Path, mock_config):
        """If a file already exists in media_dir, the stream should not be called for it."""
        cfg = mock_config

        # Pre-create the first file so it is "cached"
        cached_file = cfg.media_dir / "10_IMG_010.jpg"
        cached_file.write_bytes(b"already here")

        client = FakeClient()
        with patch("pipeline.fetch._nas.httpx.Client", return_value=client):
            fetch(cfg, FetchConfig())

        # Only item 11 should have a stream call
        stream_calls = [(m, u) for m, u, _ in client.calls if m == "STREAM"]
        stream_urls = [u for _, u in stream_calls]
        assert "/api/media/10" not in stream_urls
        assert "/api/media/11" in stream_urls


class TestFetchLivePhotoDownloadsVideo:
    def test_fetch_live_photo_downloads_video(self, tmp_path: Path, mock_config):
        """item_type=3 should trigger an extra GET with as_video=true."""
        cfg = mock_config
        client = FakeClient(collect_response=LIVE_PHOTO_RESPONSE)

        with patch("pipeline.fetch._nas.httpx.Client", return_value=client):
            manifest = fetch(cfg, FetchConfig())

        # Should have two STREAM calls: one for the photo, one for the video
        stream_calls = [(m, u, p) for m, u, p in client.calls if m == "STREAM"]
        assert len(stream_calls) == 2

        # The second stream call should have as_video param
        _, url2, params2 = stream_calls[1]
        assert params2.get("as_video") == "true"

        # Manifest entry should have live_video_path
        assert "live_video_path" in manifest[0]


class TestFetchWritesManifest:
    def test_fetch_writes_manifest(self, tmp_path: Path, mock_config):
        """manifest.json should be written at cfg.manifest_path."""
        cfg = mock_config
        client = FakeClient()

        with patch("pipeline.fetch._nas.httpx.Client", return_value=client):
            fetch(cfg, FetchConfig())

        manifest_path = cfg.manifest_path
        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text())
        assert len(data) == 2


class TestFetchManifestHasLocalPath:
    def test_fetch_manifest_has_local_path(self, tmp_path: Path, mock_config):
        """Each entry in the manifest should have a local_path key."""
        cfg = mock_config
        client = FakeClient()

        with patch("pipeline.fetch._nas.httpx.Client", return_value=client):
            manifest = fetch(cfg, FetchConfig())

        for entry in manifest:
            assert "local_path" in entry
            # local_path should be a string pointing into media_dir
            assert str(cfg.media_dir) in entry["local_path"]


class TestFetchHandlesMetaFailure:
    def test_fetch_handles_meta_failure(self, tmp_path: Path, mock_config):
        """/api/meta returns 500 — metadata should be {} in the manifest."""
        cfg = mock_config
        client = FakeClient(meta_status=500)

        with patch("pipeline.fetch._nas.httpx.Client", return_value=client):
            manifest = fetch(cfg, FetchConfig())

        # metadata should be empty dict for all entries
        for entry in manifest:
            assert entry["metadata"] == {}
