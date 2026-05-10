# Sample conversation: happy_path

_ACC1001 user pays 500 successfully via DOB factor. Exercises greeting -> account_id -> lookup success -> identity verify (DOB) -> amount -> card collection -> confirm AFFIRM -> payment success -> recap -> terminal_completed._

---

**User:** _(session start; agent greets)_

**Agent:** Hello! I'm here to help you make a payment. Could you share your account ID to get started?

**User:** Hi, I want to make a payment please

**Agent:** Hello! I'm here to help you make a payment. Could you share your account ID to get started?

**User:** ACC1001

**Agent:** I've found your account. To verify your identity, could you share your full name along with one of: date of birth, last 4 digits of Aadhaar, or pincode?

**User:** I'm Nithin Jain, my DOB is 14-05-1990

**Agent:** Thanks, Nithin! Your identity is verified. Your outstanding balance is ₹1,250.75. How much would you like to pay?

**User:** 500

**Agent:** Got it — ₹500. Now I need your card details: card number, CVV, and expiry (MM/YY or MM/YYYY).

**User:** Card 4532 0151 1283 0366, CVV 123, expires 12/27

**Agent:** I'm about to charge ₹500 to card ending 0366, expiry 12/2027. Reply 'yes' to confirm.

**User:** yes

**Agent:** Payment successful. Account: ACC1001. Amount paid: ₹500. Transaction ID: txn_1778435995263_s5rufcg. Card ending: 0366. Remaining balance: ₹750.75.

---

**Final stage:** `terminal_completed`
