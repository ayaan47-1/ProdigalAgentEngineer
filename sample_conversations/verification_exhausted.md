# Sample conversation: verification_exhausted

_tier: functionality | critical: True | result: PASS | runs: 1/1_

_captured: 2026-05-13T05:42:15.391809+00:00_

---

**User:** _(empty / bootstrap)_

**Agent:** Hello! I'm here to help you make a payment. Could you share your account ID to get started?

**User:** ACC1001

**Agent:** Account found. To verify your identity, please provide your full name and one of the following: date of birth, Aadhaar last 4 digits, or pincode.

**User:** I'm Nithin Jain, my DOB is 01-01-1980

**Agent:** The date of birth didn't match. You have 2 attempts remaining. Please try again with your full name and one of: date of birth, Aadhaar last 4 digits, or pincode.

**User:** Try DOB 02-02-1981

**Agent:** That didn't match either. You have 1 attempt remaining. Please try again with your full name and one of: date of birth, Aadhaar last 4 digits, or pincode.

**User:** Try DOB 03-03-1982

**Agent:** I wasn't able to verify your identity. For your security, this session has ended. Please contact our support team for assistance.

---

**Final snapshot.terminal:** `verification_exhausted`
