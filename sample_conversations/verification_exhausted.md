# Sample conversation: verification_exhausted

_tier: functionality | critical: True | result: PASS | runs: 1/1_

_captured: 2026-05-13T16:18:14.593003+00:00_

---

**User:** _(empty / bootstrap)_

**Agent:** Hello! I'm here to help you make a payment. Could you share your account ID to get started?

**User:** ACC1001

**Agent:** Account found. To verify your identity, please provide your full name and one of the following: date of birth, Aadhaar last 4 digits, or pincode.

**User:** I'm Nithin Jain, my DOB is 25-03-1985

**Agent:** Verification failed — the date of birth didn't match. You have 2 attempts remaining.

Please try again with your full name and one of: date of birth, Aadhaar last 4 digits, or pincode.

**User:** Try DOB 18-07-1986

**Agent:** Verification failed again — the date of birth still didn't match. You have 1 attempt remaining.

Please try with your full name and one of: date of birth, Aadhaar last 4 digits, or pincode.

**User:** Try DOB 30-11-1987

**Agent:** I wasn't able to verify your identity. For your security, this session has ended. Please contact our support team for assistance.

---

**Final snapshot.terminal:** `verification_exhausted`
