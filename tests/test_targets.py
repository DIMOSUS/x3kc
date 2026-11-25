"""Targets (x3kc/targets.py): the natives and the at-once natives of each exe, and the warnings for the others."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from x3kc import targets  # noqa: E402


class Targets(unittest.TestCase):
    def test_sets(self):
        ap, fl, xa, xf = (targets.known(t) for t in targets.TARGETS)
        self.assertIn('X2_GetModName', ap)
        self.assertNotIn('X2_GetModName', fl)
        self.assertIn('B3D_InstGetQualityModifier', fl)
        self.assertNotIn('B3D_InstGetQualityModifier', ap)
        self.assertNotIn('P_SetJoyDevice', ap | fl)
        self.assertTrue({'P_SetJoyDevice', 'X2_GetStoryBuild', 'SA_GetBulletTypeHalfZ'} <= xa & xf)
        self.assertEqual(xa - ap, targets.read('natives_x3src.txt'))
        for t in targets.TARGETS:
            self.assertLessEqual(targets.sync(t), targets.known(t), t)
        self.assertNotIn('X2_GetStoryBuild', targets.sync('ap'))
        self.assertIn('X2_GetStoryBuild', targets.sync('x3_src-ap'))
        self.assertIn('B3D_InstSetQualityModifierRecursive', targets.sync('fl'))
        self.assertEqual(targets.known(None), ap)

    def test_messages(self):
        m = targets.unknown_message
        self.assertIsNone(m('SE_ReadText', 'fl'))
        self.assertEqual(m('P_SetJoyDevice', 'ap'),
                         'native P_SetJoyDevice exists only in the x3_src engine (the original exe answers 0)')
        self.assertIsNone(m('P_SetJoyDevice', 'x3_src-fl'))
        self.assertEqual(m('B3D_InstGetQualityModifier', 'x3_src-ap'),
                         "native B3D_InstGetQualityModifier exists only in Farnham's Legacy (the AP exe answers 0)")
        self.assertIn('only in Albion Prelude', m('X2_GetModName', 'fl'))
        self.assertEqual(m('SA_NotThere', 'ap'), 'native SA_NotThere is unknown to X3AP.exe (it would return 0)')
        with self.assertRaises(ValueError):
            targets.check('tc')


if __name__ == '__main__':
    unittest.main()
