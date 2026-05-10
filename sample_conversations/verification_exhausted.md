# Sample conversation: verification_exhausted

_ACC1001 user fails verification 4 times (3 retries + final terminate per cap=3 semantics). Exercises terminal_verification_exhausted routing per DECISIONS #3._

---

**User:** _(session start; agent greets)_

**Agent:** Hello! I'm here to help you make a payment. Could you share your account ID to get started?

**User:** ACC1001

**Agent:** I've found your account. To verify your identity, could you share your full name along with one of: date of birth, last 4 digits of Aadhaar, or pincode?

**User:** I'm Nithin Jain, my DOB is 01-01-1980

**Agent:** That date of birth doesn't match. You can try again, or share a different secondary factor — Aadhaar last 4 or pincode.

**User:** Sorry, my DOB is 02-02-1981

**Agent:** That date of birth doesn't match. You can try again, or share a different secondary factor — Aadhaar last 4 or pincode.

**User:** Try DOB 03-03-1982

**Agent:** That date of birth doesn't match. You can try again, or share a different secondary factor — Aadhaar last 4 or pincode.

**User:** Last try, DOB is 04-04-1983

**Agent:** I wasn't able to verify your identity. For your security, this session has ended. Please contact our support team for assistance.

---

**Final stage:** `terminal_verification_exhausted`
