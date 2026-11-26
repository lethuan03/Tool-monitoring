# CỐ Ý FAIL để thử bộ kéo log CI. Chỉ tồn tại trên nhánh test/ci-fail, không merge.
import unittest


class CiFailDemo(unittest.TestCase):
    def test_intentional_failure(self):
        self.assertEqual(1 + 1, 3, "lỗi cố ý từ nhánh test/ci-fail")
