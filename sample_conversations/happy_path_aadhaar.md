# Sample conversation: happy_path_aadhaar

_tier: functionality | critical: True | result: PASS | runs: 1/1_

_captured: 2026-05-13T16:12:45.475442+00:00_

---

**User:** _(empty / bootstrap)_

**Agent:** Hello! I'm here to help you make a payment. Could you share your account ID to get started?

**User:** ACC1001

**Agent:** Account found. To verify your identity, please provide your full name and one of the following: date of birth, Aadhaar last 4 digits, or pincode.

**User:** I'm Nithin Jain. The last 4 digits of my Aadhaar are 4321.

**Agent:** Thanks, Nithin Jain! Your identity is verified. Your outstanding balance is ₹1,250.75.

How much would you like to pay today?

**User:** 500

**Agent:** Please provide your card details: card number, CVV, and expiry date (MM/YY).

**User:** Card 4532 0151 1283 0366, CVV 123, expires 12/27

**Agent:** I'm about to charge ₹500.00 to card ending 0366, expiry 12/2027. Reply 'yes' to confirm.

**User:** yes

**Agent:** Payment successful. Account: ACC1001. Amount paid: ₹500.00. Transaction ID: txn_1778688761583_9ozhece. Card ending: 0366. Remaining balance: ₹750.75.

---

**Final snapshot.terminal:** `completed`
