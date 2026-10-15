import unittest
import sys
if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName('tests.test_maintenance_content'))
    sys.exit(0 if result.wasSuccessful() else 1)
