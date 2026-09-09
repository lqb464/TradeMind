# Training data

Keep only small, licensed examples in Git. Raw vendor data and generated model
artifacts belong outside version control.

Price CSVs must contain `date,open,high,low,close,volume` with unique,
chronologically sortable timestamps. Sentiment CSVs default to `text,label`.
Retriever JSONL uses one `query` and one `positive` passage per line.

Every dataset used for a release model should record its license, source,
retrieval timestamp, point-in-time guarantees and corporate-action policy.
