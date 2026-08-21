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

    def test_photo_urls_use_the_same_documents_path_prefix_as_contacts(
        self,
    ) -> None:
        """
        The three PHP photo handlers (get/replace/remove) build their
        backend URL as DOCUMENTS_PATH + "/" + document_id +
        "/contacts/" + contact_id + "/photo" - the SAME
        "/api/v1/admin/documents" prefix the sibling contact
        list/add/update/delete routes use. This is a structural fact
        about the PHP source, not a live request - it does not by
        itself prove the backend route registers with a matching
        prefix (see backend/tests/test_admin_contact_photos.py's own
        test_photo_route_paths_share_the_documents_prefix for that
        side), but it locks in the ONE fact that was wrong before this
        fix: this proxy silently building URLs with a DIFFERENT prefix
        than DOCUMENTS_PATH (e.g. a hand-rolled "/api/v1/admin" string
        instead of the shared constant) is exactly the class of drift
        that caused every Admin photo request to 404.
        """

        for marker in (
            "handle_get_contact_photo",
            "handle_replace_contact_photo",
            "handle_remove_contact_photo",
        ):
            # The bare method name also appears earlier, in the
            # add_action(...) registration callback array - search
            # for the actual "function <name>" definition instead.
            start = PHP.index(f"function {marker}")
            next_boundaries = [
                PHP.index(needle, start + 1)
                for needle in (
                    "\n    public static function ",
                    "\n    private static function ",
                    "\n    protected static function ",
                )
                if needle in PHP[start + 1:]
            ]
            end = min(next_boundaries)
            handler_source = PHP[start:end]

            self.assertIn(
                "self::DOCUMENTS_PATH",
                handler_source,
                f"{marker} must build its backend URL from "
                "self::DOCUMENTS_PATH, the same constant every other "
                "Admin contact route uses",
            )


if __name__ == "__main__":
    unittest.main()
