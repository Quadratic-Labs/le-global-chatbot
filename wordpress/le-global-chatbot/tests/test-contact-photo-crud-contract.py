from pathlib import Path
import unittest

R = Path("wordpress/le-global-chatbot")
JS = (R / "assets/admin.js").read_text()
PHP = (
    R / "includes/class-le-global-chatbot-admin.php"
).read_text()

class ContactPhotoCrudContract(unittest.TestCase):

    def test_admin_has_preview(self):
        self.assertIn("contact-card-photo", JS)
        self.assertIn("contact-photo-preview", JS)

    def test_admin_has_optional_file_input(self):
        self.assertIn(
            'input.accept = "image/jpeg,image/png,image/webp"',
            JS,
        )

    def test_edit_upload_exists(self):
        self.assertIn("uploadContactPhoto(", JS)
        self.assertIn(
            "le_global_admin_contact_photo_replace",
            JS,
        )

    def test_remove_exists(self):
        self.assertIn(
            "le_global_admin_contact_photo_remove",
            JS,
        )
        self.assertIn("removeCurrentContactPhoto", JS)

    def test_binary_preview_is_admin_only(self):
        self.assertIn(
            "wp_ajax_le_global_admin_contact_photo_get",
            PHP,
        )
        self.assertNotIn(
            "wp_ajax_nopriv_le_global_admin_contact_photo_get",
            PHP,
        )

    def test_php_validates_mime_and_size(self):
        self.assertIn("10 * 1024 * 1024", PHP)
        self.assertIn("image/webp", PHP)

    def test_backend_never_receives_filename(self):
        self.assertNotIn("photo_filename", PHP)


if __name__ == "__main__":
    unittest.main()
