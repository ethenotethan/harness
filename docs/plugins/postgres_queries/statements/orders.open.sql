-- params: {"state": {"type": "enum", "values": ["open", "closed", "refunded"], "default": "open"},
--          "limit": {"type": "int", "min": 1, "max": 500, "default": 100}}
SELECT id, customer, total, created_at
FROM orders
WHERE state = %(state)s
ORDER BY created_at DESC
LIMIT %(limit)s;
