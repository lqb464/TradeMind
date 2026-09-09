# Data workflow

Store compact, licensed sample datasets only. Keep raw vendor data, user uploads,
SQLite/runtime data, and generated model artifacts outside version control.

Price CSV format: `date,open,high,low,close,volume`; timestamps must be unique
and sortable. Sentiment CSV defaults to `text,label`.

For each research dataset, record source, license, retrieval timestamp,
point-in-time guarantees, adjustment policy, and any known limitations.
