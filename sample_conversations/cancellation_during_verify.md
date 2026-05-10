# Sample conversation: cancellation_during_verify

_ACC1001 user politely refuses to share DOB after lookup. Exercises NEGATE intent classification + cancel_guard + terminal_cancelled routing per DECISIONS #10. Cancellation does not consume retry budget._

---

**User:** _(session start; agent greets)_

**Agent:** Hello! I'm here to help you make a payment. Could you share your account ID to get started?

**User:** ACC1001

**Agent:** I've found your account. To verify your identity, could you share your full name along with one of: date of birth, last 4 digits of Aadhaar, or pincode?

**User:** I'd rather not share my DOB

**Agent:** Session cancelled. No payment processed.

---

**Final stage:** `terminal_cancelled`
