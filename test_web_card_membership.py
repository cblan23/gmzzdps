import sqlite3
import unittest
from server import web_card_membership as m


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        m.initialize(self.db)
        self.openid='A'*32
        self.id=m.register_verified_identity(self.db,'123456',self.openid)

    def test_pending_cannot_claim(self):
        with self.assertRaises(PermissionError):m.claim_identity(self.db,'123456',self.openid,10)

    def test_any_one_group_and_expiry(self):
        for group in m.ALLOWED_GROUPS:
            m.approve(self.db,self.id,'1806525',group,'admin',10,100)
            self.assertEqual(m.claim_identity(self.db,'123456',self.openid,99),('1806525',group))
        with self.assertRaises(PermissionError):m.claim_identity(self.db,'123456',self.openid,100)

    def test_no_rebind_or_second_identity_for_same_qq(self):
        m.approve(self.db,self.id,'1806525','165966739','admin',10,100)
        second=m.register_verified_identity(self.db,'123456','B'*32)
        with self.assertRaises(sqlite3.IntegrityError):m.approve(self.db,second,'1806525','165966739','admin',10,100)
        with self.assertRaises(ValueError):m.approve(self.db,self.id,'1234567','165966739','admin',10,100)

    def test_revoked_and_other_app_cannot_claim(self):
        m.approve(self.db,self.id,'1806525','165966739','admin',10,100)
        with self.assertRaises(PermissionError):m.claim_identity(self.db,'654321',self.openid,20)
        m.revoke(self.db,self.id,'admin',20)
        with self.assertRaises(PermissionError):m.claim_identity(self.db,'123456',self.openid,21)

    def test_foreign_group_rejected_and_identity_idempotent(self):
        self.assertEqual(self.id,m.register_verified_identity(self.db,'123456',self.openid.lower()))
        with self.assertRaises(ValueError):m.approve(self.db,self.id,'1806525','999999','admin',10,100)

if __name__=='__main__':unittest.main()
