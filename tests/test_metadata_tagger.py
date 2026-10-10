"""Unit and integration tests for multi-source metadata aggregation and unified tagging."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from mutagen.id3 import ID3
from mutagen.flac import FLAC
from mutagen.oggopus import OggOpus
from mutagen.mp4 import MP4

from services.metadata import (
    DeezerProvider,
    DiscogsProvider,
    ITunesProvider,
    MetadataCandidate,
    MusicBrainzProvider,
    SpotifyProvider,
    UnifiedAlbumMetadata,
    UnifiedTrackMetadata,
    calculate_string_similarity,
    compute_confidence_score,
    compute_metadata_completeness,
    fetch_lrclib_lyrics_async,
    rank_and_recommend_candidates,
    search_album_metadata_candidates,
    search_metadata,
    search_track_metadata_candidates,
)
from services.tagger import (
    apply_unified_metadata_to_album,
    apply_unified_metadata_to_file,
    extract_cover_bytes,
    read_tags,
    write_loose_cover,
    write_tags,
)


class TestMetadataModelsAndScoring(unittest.TestCase):
    def test_unified_metadata_serialization(self):
        track = UnifiedTrackMetadata(
            title="Sallim",
            artist="Mohamed Mounir",
            album="Ahmar Shafayef",
            track_number=1,
            total_tracks=10,
            disc_number=1,
            year="2003",
            genre="Nubian Pop",
            genres=["Nubian Pop", "Arabic"],
            producers=["Tarek Madkour"],
            composers=["Ahmed Mounir"],
            lyrics_unsynced="Lyrics text here",
            duration_seconds=245.5,
            source="Deezer",
            source_id="123456",
        )
        d = track.to_dict()
        self.assertEqual(d["title"], "Sallim")
        self.assertEqual(d["producers"], ["Tarek Madkour"])
        self.assertNotIn("cover_bytes", d)

        # Restore from dict
        restored = UnifiedTrackMetadata.from_dict(d)
        self.assertEqual(restored.title, "Sallim")
        self.assertEqual(restored.composers, ["Ahmed Mounir"])

        album = UnifiedAlbumMetadata(
            album="Ahmar Shafayef",
            artist="Mohamed Mounir",
            year="2003",
            total_tracks=1,
            tracks=[track],
            source="Deezer",
        )
        alb_dict = album.to_dict()
        self.assertEqual(alb_dict["album"], "Ahmar Shafayef")
        self.assertEqual(len(alb_dict["tracks"]), 1)

        restored_alb = UnifiedAlbumMetadata.from_dict(alb_dict)
        self.assertEqual(len(restored_alb.tracks), 1)
        self.assertEqual(restored_alb.tracks[0].title, "Sallim")

    def test_string_similarity(self):
        # Exact match
        self.assertAlmostEqual(calculate_string_similarity("Amr Diab", "Amr Diab"), 1.0)
        # Case and whitespace tolerance
        self.assertAlmostEqual(calculate_string_similarity("amr diab", "Amr Diab"), 1.0)
        # Substring containment
        sim = calculate_string_similarity("Mohamed Mounir", "Mohamed Mounir (Live)")
        self.assertGreater(sim, 0.8)
        # Completely different
        diff = calculate_string_similarity("The Beatles", "Fairuz")
        self.assertLess(diff, 0.3)

    def test_confidence_and_completeness_scoring(self):
        # High confidence match
        score_high = compute_confidence_score(
            query_title_or_album="Kol Hayaty",
            query_artist="Amr Diab",
            cand_title_or_album="Kol Hayaty",
            cand_artist="Amr Diab",
            local_track_count=10,
            cand_track_count=10,
            local_duration=210.0,
            cand_duration=211.0,
        )
        self.assertGreaterEqual(score_high, 95.0)

        # Completeness scoring
        rich_album = UnifiedAlbumMetadata(
            album="Test",
            artist="Artist",
            cover_url="https://example.com/cover.jpg",
            total_tracks=10,
            year="2020",
            genres=["Pop"],
            producers=["Producer 1"],
            composers=["Composer 1"],
            tracks=[
                UnifiedTrackMetadata(
                    title="T1", lyrics_synced="[00:01.00] Synced"
                )
            ],
        )
        compl = compute_metadata_completeness(rich_album)
        self.assertGreaterEqual(compl, 90.0)

    def test_recommendation_ranking(self):
        cand1 = MetadataCandidate(
            source="ProviderBasic",
            confidence_score=70.0,
            is_recommended=False,
            album_data=UnifiedAlbumMetadata(album="Album", artist="Artist"),
        )
        cand2 = MetadataCandidate(
            source="ProviderRich",
            confidence_score=85.0,
            is_recommended=False,
            album_data=UnifiedAlbumMetadata(
                album="Album",
                artist="Artist",
                cover_url="https://img.jpg",
                total_tracks=10,
                producers=["Producer A"],
                composers=["Composer B"],
                tracks=[UnifiedTrackMetadata(title="Song", duration_seconds=180.0)],
            ),
        )

        ranked = rank_and_recommend_candidates([cand1, cand2])
        self.assertEqual(ranked[0].source, "ProviderRich")
        self.assertTrue(ranked[0].is_recommended)
        self.assertFalse(ranked[1].is_recommended)

    def test_arabic_genre_resolution(self):
        import re
        from utils.helpers import is_arabic_music, normalize_genre, resolve_fallback_genre
        self.assertTrue(is_arabic_music("Elissa", "Saharna Ya Leil"))
        self.assertTrue(is_arabic_music("Amr Diab", "Kol Hayaty"))
        self.assertTrue(is_arabic_music("إليسا", "سهرنا يا ليل"))
        self.assertFalse(is_arabic_music("Coldplay", "Parachutes"))
        self.assertFalse(is_arabic_music("PinkPantheress", "Fancy that"))

        # Never return 'Music' or generic
        self.assertEqual(resolve_fallback_genre("Elissa", "Saharna Ya Leil", "Music"), "Arabic Pop")
        self.assertEqual(resolve_fallback_genre("Elissa", "Saharna Ya Leil", ""), "Arabic Pop")
        self.assertEqual(resolve_fallback_genre("Amr Diab", "Wayah", None), "Arabic Pop")
        self.assertEqual(resolve_fallback_genre("Coldplay", "Yellow", "Music"), "Pop")
        self.assertEqual(resolve_fallback_genre("Coldplay", "Yellow", "Alternative Rock"), "Alternative Rock")

        # PinkPantheress "Fancy that" localized iTunes Arabic genre translated to English
        self.assertEqual(normalize_genre("موسيقى البوب"), "Pop")
        self.assertEqual(resolve_fallback_genre("PinkPantheress", "Fancy that", "موسيقى البوب"), "Pop")

        # Arabic artists: tags must strictly remain in English
        self.assertEqual(resolve_fallback_genre("Elissa", "Saharna Ya Leil", "موسيقى البوب"), "Pop")
        self.assertEqual(resolve_fallback_genre("Elissa", "Saharna Ya Leil", "طرب"), "Tarab")
        self.assertEqual(resolve_fallback_genre("Ahmed Saad", "Ekhtyaraty", "شعبي"), "Shaabi")
        self.assertEqual(resolve_fallback_genre("Hassan Shakosh", "Bent El Giran", "مهرجانات"), "Mahraganat")
        self.assertEqual(resolve_fallback_genre("Amr Diab", "Saharna Ya Lail", "البوب"), "Pop")
        self.assertEqual(resolve_fallback_genre("Cairokee", "Roma", "موسيقى الروك"), "Rock")
        self.assertEqual(resolve_fallback_genre("Wegz", "El Bakht", "موسيقى الراب"), "Hip-Hop/Rap")

        # Deduplication and mixed strings
        self.assertEqual(normalize_genre("Pop / موسيقى البوب"), "Pop")
        self.assertEqual(normalize_genre("Pop (موسيقى البوب)"), "Pop")
        self.assertEqual(normalize_genre("Rock / روك"), "Rock")
        self.assertEqual(normalize_genre("Pop, Rock"), "Pop, Rock")

        # Verify absolutely no Arabic characters exist in any resolved genre
        for test_case in [
            ("PinkPantheress", "Fancy that", "موسيقى البوب"),
            ("Elissa", "Saharna Ya Leil", "طرب"),
            ("Amr Diab", "Kol Hayaty", "بوب"),
            ("Cairokee", "Abnaa El Batta El Soda", "موسيقى الروك"),
            ("Fairuz", "Kifak Enta", "موسيقى عربية"),
        ]:
            res = resolve_fallback_genre(test_case[0], test_case[1], test_case[2])
            self.assertIsNone(
                re.search(r"[\u0600-\u06FF]", res),
                f"Resolved genre '{res}' contains Arabic characters for {test_case}",
            )

    def test_track_global_alignment_order_inversion(self):
        from services.tagger import align_album_tracks_globally
        # Emulate Saharna Ya Leil where CD sequence and digital release sequence differ
        with tempfile.TemporaryDirectory() as tmp_dir:
            f1 = Path(tmp_dir) / "01 - Aaks Elli Shayfenha.mp3"
            f1.write_bytes(b"ID3" + b"\x00" * 50)
            f2 = Path(tmp_dir) / "02 - Maktooba Leek.mp3"
            f2.write_bytes(b"ID3" + b"\x00" * 50)
            f3 = Path(tmp_dir) / "03 - Saharna Ya Leil.mp3"
            f3.write_bytes(b"ID3" + b"\x00" * 50)

            # Candidate tracklist where track 1 is Saharna Ya Leil, track 2 is Maktooba Leek, track 3 is Aaks Elli Shayfenha
            candidate_tracks = [
                UnifiedTrackMetadata(title="Saharna Ya Leil", track_number=1, duration_seconds=258),
                UnifiedTrackMetadata(title="Maktooba Leek", track_number=2, duration_seconds=312),
                UnifiedTrackMetadata(title="Aaks Elli Shayfenha", track_number=3, duration_seconds=265),
            ]

            aligned = align_album_tracks_globally([f1, f2, f3], candidate_tracks)
            matched_dict = {p.name: t.title for p, t in aligned}

            # File 01 should match "Aaks Elli Shayfenha" despite file index 1 vs track 3
            self.assertEqual(matched_dict["01 - Aaks Elli Shayfenha.mp3"], "Aaks Elli Shayfenha")
            self.assertEqual(matched_dict["02 - Maktooba Leek.mp3"], "Maktooba Leek")
            self.assertEqual(matched_dict["03 - Saharna Ya Leil.mp3"], "Saharna Ya Leil")


class TestProvidersMocked(unittest.IsolatedAsyncioTestCase):
    async def test_deezer_album_search_mock(self):
        mock_search_data = {
            "data": [
                {
                    "id": 12345,
                    "title": "Mock Album",
                    "cover_xl": "https://deezer.com/cover_xl.jpg",
                    "artist": {"name": "Mock Artist"},
                }
            ]
        }
        mock_detail_data = {
            "id": 12345,
            "title": "Mock Album",
            "release_date": "2021-01-01",
            "nb_tracks": 1,
            "cover_xl": "https://deezer.com/cover_xl.jpg",
            "artist": {"name": "Mock Artist"},
            "genres": {"data": [{"name": "Pop"}]},
            "contributors": [{"name": "Mock Producer", "role": "Producer"}],
            "tracks": {
                "data": [
                    {
                        "id": 999,
                        "title": "Track 1",
                        "track_position": 1,
                        "disk_number": 1,
                        "duration": 200,
                    }
                ]
            },
        }

        mock_resp_search = MagicMock(status_code=200, json=lambda: mock_search_data)
        mock_resp_detail = MagicMock(status_code=200, json=lambda: mock_detail_data)

        with patch("httpx.AsyncClient.get", side_effect=[mock_resp_search, mock_resp_detail]):
            results = await DeezerProvider.search_album("Mock Album", "Mock Artist")
            self.assertEqual(len(results), 1)
            alb = results[0]
            self.assertEqual(alb.album, "Mock Album")
            self.assertEqual(alb.artist, "Mock Artist")
            self.assertEqual(alb.producers, ["Mock Producer"])
            self.assertEqual(len(alb.tracks), 1)
            self.assertEqual(alb.tracks[0].duration_seconds, 200.0)

    async def test_itunes_album_search_mock(self):
        mock_search_data = {
            "results": [
                {
                    "collectionId": 777,
                    "collectionName": "Apple Album",
                    "artistName": "Apple Artist",
                    "artworkUrl100": "https://mzstatic.com/100x100bb.jpg",
                    "releaseDate": "2022-05-01T00:00:00Z",
                    "trackCount": 1,
                    "primaryGenreName": "Rock",
                }
            ]
        }
        mock_lookup_data = {
            "results": [
                {"wrapperType": "collection"},
                {
                    "wrapperType": "track",
                    "trackId": 888,
                    "trackName": "Apple Track",
                    "trackNumber": 1,
                    "trackCount": 1,
                    "discNumber": 1,
                    "discCount": 1,
                    "trackTimeMillis": 180000,
                    "primaryGenreName": "Rock",
                },
            ]
        }

        mock_resp_search = MagicMock(status_code=200, json=lambda: mock_search_data)
        mock_resp_lookup = MagicMock(status_code=200, json=lambda: mock_lookup_data)

        with patch("httpx.AsyncClient.get", side_effect=[mock_resp_search, mock_resp_lookup]):
            results = await ITunesProvider.search_album("Apple Album", "Apple Artist")
            self.assertEqual(len(results), 1)
            alb = results[0]
            self.assertEqual(alb.album, "Apple Album")
            self.assertIn("1200x1200bb.jpg", alb.cover_url)
            self.assertEqual(len(alb.tracks), 1)
            self.assertEqual(alb.tracks[0].title, "Apple Track")
            self.assertEqual(alb.tracks[0].duration_seconds, 180.0)


class TestUnifiedTaggerEngine(unittest.TestCase):
    def test_apply_unified_metadata_to_mp3(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "01 - Test.mp3"
            file_path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 100)

            track_meta = UnifiedTrackMetadata(
                title="Unified Title",
                artist="Unified Artist",
                album_artist="Unified Album Artist",
                album="Unified Album",
                track_number=2,
                total_tracks=10,
                disc_number=1,
                total_discs=1,
                year="2024",
                genre="Electro",
                composers=["Composer One", "Composer Two"],
                lyricists=["Lyricist One"],
                producers=["Producer Main"],
                arrangers=["Arranger Main"],
                lyrics_unsynced="Test Lyrics Line 1\nLine 2",
            )
            fake_cover = b"fake_jpeg_cover_bytes"

            success = apply_unified_metadata_to_file(file_path, track_meta, cover_bytes=fake_cover)
            self.assertTrue(success)

            # Inspect written ID3 frames
            id3 = ID3(str(file_path))
            self.assertEqual(str(id3["TIT2"].text[0]), "Unified Title")
            self.assertEqual(str(id3["TPE1"].text[0]), "Unified Artist")
            self.assertEqual(str(id3["TPE2"].text[0]), "Unified Album Artist")
            self.assertEqual(str(id3["TALB"].text[0]), "Unified Album")
            self.assertEqual(str(id3["TRCK"].text[0]), "2/10")
            self.assertEqual(str(id3["TPOS"].text[0]), "1/1")
            self.assertEqual(str(id3["TDRC"].text[0]), "2024")
            self.assertEqual(str(id3["TCON"].text[0]), "Electro")
            self.assertEqual(str(id3["TCOM"].text[0]), "Composer One, Composer Two")
            self.assertEqual(str(id3["TEXT"].text[0]), "Lyricist One")
            self.assertIn("Producer Main", str(id3.get("TXXX:PRODUCER", "")))
            self.assertEqual(id3.getall("USLT")[0].text, "Test Lyrics Line 1\nLine 2")
            self.assertEqual(id3["APIC:Cover"].data, fake_cover)

    def test_apply_unified_metadata_translates_arabic_genre_to_english(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "01 - Fancy That.mp3"
            file_path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 100)

            # Metadata with localized Arabic genre e.g. from iTunes
            track_meta = UnifiedTrackMetadata(
                title="Fancy that",
                artist="PinkPantheress",
                album="Fancy that",
                genre="موسيقى البوب",
                genres=["موسيقى البوب"],
            )

            success = apply_unified_metadata_to_file(file_path, track_meta)
            self.assertTrue(success)

            # Inspect written ID3 frames: TCON must be Pop in English!
            id3 = ID3(str(file_path))
            self.assertEqual(str(id3["TCON"].text[0]), "Pop")
            self.assertEqual(track_meta.genre, "Pop")

    def test_apply_unified_metadata_to_flac(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "01 - Test.flac"
            file_path.write_bytes(b"dummy flac")

            track_meta = UnifiedTrackMetadata(
                title="FLAC Title",
                artist="FLAC Artist",
                album="FLAC Album",
                track_number=1,
                total_tracks=5,
                year="2023",
                genre="Acoustic",
                composers=["FLAC Composer"],
                producers=["FLAC Producer"],
                lyrics_unsynced="FLAC lyrics",
            )

            fake_flac_tags = {}
            mock_flac = MagicMock()
            mock_flac.__setitem__ = lambda self, k, v: fake_flac_tags.__setitem__(k, v)
            mock_flac.__getitem__ = lambda self, k: fake_flac_tags.__getitem__(k)
            mock_flac.save = MagicMock()
            mock_flac.clear_pictures = MagicMock()
            mock_flac.add_picture = MagicMock()

            with patch("services.tagger.FLAC", return_value=mock_flac):
                success = apply_unified_metadata_to_file(file_path, track_meta, cover_bytes=b"flac_cover")
                self.assertTrue(success)
                self.assertEqual(fake_flac_tags.get("title"), ["FLAC Title"])
                self.assertEqual(fake_flac_tags.get("artist"), ["FLAC Artist"])
                self.assertEqual(fake_flac_tags.get("composer"), ["FLAC Composer"])
                self.assertEqual(fake_flac_tags.get("producer"), ["FLAC Producer"])
                self.assertEqual(fake_flac_tags.get("lyrics"), ["FLAC lyrics"])
                mock_flac.add_picture.assert_called()
                mock_flac.save.assert_called()

    def test_apply_unified_metadata_to_album_folder(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            album_dir = Path(tmp_dir) / "My Album"
            album_dir.mkdir(parents=True)

            f1 = album_dir / "01 - Track One.mp3"
            f1.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 100)
            f2 = album_dir / "02 - Track Two.mp3"
            f2.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 100)

            album_meta = UnifiedAlbumMetadata(
                album="Greatest Hits",
                artist="Legend",
                year="2025",
                genre="Classics",
                total_tracks=2,
                cover_bytes=b"album_cover_bytes",
                tracks=[
                    UnifiedTrackMetadata(
                        title="Track One",
                        track_number=1,
                        lyrics_synced="[00:00.00] Synced line",
                    ),
                    UnifiedTrackMetadata(
                        title="Track Two",
                        track_number=2,
                    ),
                ],
            )

            with patch("services.tagger.fetch_and_save_lrc", return_value=True):
                result = apply_unified_metadata_to_album(album_dir, album_meta)

            self.assertEqual(result["album"], "Greatest Hits")
            self.assertEqual(result["artist"], "Legend")

            # Check loose cover.jpg was written
            cover_file = album_dir / "cover.jpg"
            self.assertTrue(cover_file.exists())
            self.assertEqual(cover_file.read_bytes(), b"album_cover_bytes")

            # Check companion .lrc for track one was created from synced lyrics
            lrc_file = album_dir / "01 - Track One.lrc"
            self.assertTrue(lrc_file.exists())
            self.assertEqual(lrc_file.read_text(encoding="utf-8"), "[00:00.00] Synced line")

    def test_granular_read_and_write_tags_single_file(self):
        """Tests read_tags, granular write_tags, and extract_cover_bytes on audio files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "01 - Test Song.mp3"
            file_path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 100)

            # 1. Initial write with initial tags
            initial_fields = {
                "title": "Original Title",
                "artist": "Original Artist",
                "album": "Original Album",
                "genre": "Acoustic",
                "track_number": 1,
                "total_tracks": 10,
            }
            ok = write_tags(file_path, initial_fields)
            self.assertTrue(ok)

            # 2. Inspect tags using read_tags
            tags = read_tags(file_path)
            self.assertEqual(tags["title"], "Original Title")
            self.assertEqual(tags["artist"], "Original Artist")
            self.assertEqual(tags["album"], "Original Album")
            self.assertEqual(tags["track_number"], 1)

            # 3. Update ONLY producer, genre, and synced lyrics (leaving title & artist untouched)
            update_fields = {
                "genre": "EDM",
                "producers": ["Master Producer"],
                "lyrics_synced": "[00:01.00] Hook line",
            }
            ok_update = write_tags(file_path, update_fields, cover_bytes=b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 20)
            self.assertTrue(ok_update)

            # 4. Re-read tags and verify precision:
            updated = read_tags(file_path)
            # Unedited tags remain intact
            self.assertEqual(updated["title"], "Original Title")
            self.assertEqual(updated["artist"], "Original Artist")
            self.assertEqual(updated["album"], "Original Album")
            # Updated tags are reflected
            self.assertEqual(updated["genre"], "EDM")
            self.assertEqual(updated["producers"], ["Master Producer"])
            self.assertEqual(updated["lyrics_synced"], "[00:01.00] Hook line")
            self.assertTrue(updated["has_cover"])
            self.assertTrue(updated["has_lrc"])

            # 5. Verify extract_cover_bytes returns artwork
            cover_data = extract_cover_bytes(file_path)
            self.assertIsNotNone(cover_data)
            self.assertTrue(cover_data[0].startswith(b"\xff\xd8"))

    def test_search_metadata_function(self):
        """Tests search_metadata returns ranked candidates with confidence and autofill fields."""
        mock_candidates = [
            MetadataCandidate(
                source="MusicBrainz",
                confidence_score=94.5,
                is_recommended=True,
                track_data=UnifiedTrackMetadata(
                    title="Tamally Maak",
                    artist="Amr Diab",
                    album="Tamally Maak",
                    year="2000",
                    genre="Arabic Pop",
                    producers=["Tarek Madkour"],
                    composers=["Sherif Tag"],
                ),
                preview={
                    "title": "Tamally Maak",
                    "artist": "Amr Diab",
                    "album": "Tamally Maak",
                    "year": "2000",
                    "genres": "Arabic Pop",
                    "has_cover": False,
                    "has_producers": True,
                    "has_composers": True,
                    "has_lyrics": False,
                    "confidence_percent": 95,
                },
            )
        ]

        with patch("services.metadata.search_track_metadata_candidates_async", AsyncMock(return_value=mock_candidates)):
            with patch("services.metadata.fetch_lrclib_lyrics_async", AsyncMock(return_value={"synced": "[00:05.00] Tamally maak", "plain": "Tamally maak"})):
                results = search_metadata("Tamally Maak", type="track", artist="Amr Diab")

        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)
        top = results[0]
        self.assertEqual(top["source"], "MusicBrainz")
        self.assertTrue(top["is_recommended"])
        self.assertIn("fields", top)
        self.assertEqual(top["fields"]["title"], "Tamally Maak")
        self.assertEqual(top["fields"]["artist"], "Amr Diab")
        self.assertEqual(top["fields"]["producers"], "Tarek Madkour")
        self.assertEqual(top["fields"]["lyrics_synced"], "[00:05.00] Tamally maak")


    def test_id3v24_tipl_and_sylt_tagging(self):
        """Verifies that ID3v2.4 correctly writes TIPL (involved people) and SYLT frames."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "track.mp3"
            file_path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 1024)

            payload = {
                "title": "Song v2.4",
                "artist": "Artist",
                "producers": ["Top Producer"],
                "arrangers": ["Master Arranger"],
                "lyrics_synced": "[00:10.50] Hello world\n[00:15.00] Second line",
            }
            ok = write_tags(file_path, payload)
            self.assertTrue(ok)

            id3 = ID3(str(file_path))
            # Verify TIPL or IPLS frame present
            tipls = id3.getall("TIPL")
            ipls = id3.getall("IPLS")
            self.assertTrue(len(tipls) > 0 or len(ipls) > 0)

            # Verify SYLT frame present
            sylts = id3.getall("SYLT")
            self.assertEqual(len(sylts), 1)
            self.assertEqual(len(sylts[0].text), 2)
            self.assertEqual(sylts[0].text[0][0], "Hello world")
            self.assertEqual(sylts[0].text[0][1], 10500)


if __name__ == "__main__":
    unittest.main()

