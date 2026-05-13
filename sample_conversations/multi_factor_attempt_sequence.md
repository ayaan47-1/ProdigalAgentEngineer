# Sample conversation: multi_factor_attempt_sequence

_tier: functionality | critical: False | result: PASS | runs: 1/1_

_captured: 2026-05-13T05:43:29.377599+00:00_

---

**User:** _(empty / bootstrap)_

**Agent:** Hello! I'm here to help you make a payment. Could you share your account ID to get started?

**User:** ACC1001

**Agent:** Account found. To verify your identity, please provide your full name and one of the following: date of birth, Aadhaar last 4 digits, or pincode.

**User:** I'm Nithin Jain. My DOB is 01-01-1980

**Agent:** The DOB didn't match. You have 2 attempts remaining. Please try again with your full name and one of: date of birth, Aadhaar last 4 digits, or pincode.

**User:** Try pincode 999999 instead

**Agent:** The pincode didn't match either. You have 1 attempt remaining. Please provide your full name and one of: date of birth, Aadhaar last 4 digits, or pincode.

**User:** Sorry, the correct DOB is 1990-05-14

**Agent:** Thanks, Nithin Jain! Your identity is verified. Your outstanding balance is ₹1,250.75.

How much would you like to pay today?

**User:** 500

**Agent:** Please provide your card details: card number, CVV, and expiry date (MM/YY).

**User:** Card 4532 0151 1283 0366, CVV 123, expires 12/27

**Agent:** I'm about to charge ₹500.00 to card ending 0366, expiry 12/2027. Reply 'yes' to confirm.

**User:** yes

**Agent:** Payment successful. Account: ACC1001. Amount paid: ₹500.00. Transaction ID: txn_1778651005722_34ph18s. Card ending: 0366. Remaining balance: ₹750.75.

---

**Final snapshot.terminal:** `completed`
