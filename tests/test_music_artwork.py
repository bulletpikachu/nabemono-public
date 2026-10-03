import unittest
from src.music.youtube import track_from_youtube_entry, youtube_artwork

class ArtworkTests(unittest.TestCase):
    def test_metadata_thumbnail_and_artist(self):
        track = track_from_youtube_entry({'id': 'dQw4w9WgXcQ', 'title': 'Track', 'uploader': 'Artist', 'thumbnail': 'https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg'})
        self.assertEqual('Artist', track.artist)
        self.assertTrue(track.artwork_url.endswith('/maxresdefault.jpg'))

    def test_flat_playlist_thumbnail_and_url_fallback(self):
        url = 'https://youtu.be/dQw4w9WgXcQ'
        self.assertEqual('https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg', youtube_artwork({}, url))
        self.assertEqual('https://i.ytimg.com/picture.jpg', youtube_artwork({'thumbnails': [{'url': 'https://i.ytimg.com/picture.jpg'}]}, url))

    def test_rejects_untrusted_metadata_urls(self):
        for value in ['https://attacker.example/image', 'http://i.ytimg.com/image', 'https://i.ytimg.com.attacker.example/image', 'javascript:alert(1)']:
            self.assertIsNone(youtube_artwork({'thumbnail': value}, 'https://youtu.be/invalid'))
