# Sample conversation: insufficient_balance_recovery

_ACC1001 user attempts to pay 2000 (exceeds balance of 1250.75) -> stub returns insufficient_balance -> agent re-prompts for a smaller amount -> 500 succeeds. Exercises DECISIONS #4 unbounded re-prompt path: insufficient_balance does NOT consume payment_retries budget. Replaces the originally-planned typo-class recovery persona because the lookup-account stub appears to accept any well-formed CVV — the API-driven INVALID_CVV path is exercised in test_agent.py and test_state_transitions.py via mocked api responses; this persona covers the broader 'payment failure -> user corrects -> success' loop end-to-end against the real stub._

---

**User:** _(session start; agent greets)_

**Agent:** Hello! I'm here to help you make a payment. Could you share your account ID to get started?

**User:** ACC1001

**Agent:** I've found your account. To verify your identity, could you share your full name along with one of: date of birth, last 4 digits of Aadhaar, or pincode?

**User:** Nithin Jain, DOB 14-05-1990

**Agent:** Thanks, Nithin! Your identity is verified. Your outstanding balance is ₹1,250.75. How much would you like to pay?

**User:** 2000

**Agent:** Got it — ₹2000. Now I need your card details: card number, CVV, and expiry (MM/YY or MM/YYYY).

**User:** Card 4532 0151 1283 0366, CVV 123, expires 12/27

**Agent:** I'm about to charge ₹2000 to card ending 0366, expiry 12/2027. Reply 'yes' to confirm.

**User:** yes

**Agent:** Your account doesn't have enough balance for that amount. Could you share a smaller amount?

**User:** 500

**Agent:** I'm about to charge ₹500 to card ending 0366, expiry 12/2027. Reply 'yes' to confirm.

**User:** yes

**Agent:** Payment successful. Account: ACC1001. Amount paid: ₹500. Transaction ID: txn_1778436017821_hltp7ot. Card ending: 0366. Remaining balance: ₹750.75.

---

**Final stage:** `terminal_completed`
