"""Evening news digest from public sources: cannabis and psychedelic science and therapy,
policy and industry, community, reggae and dub.

Pipeline:
1. sources.py lists what is read and how often; adapters.py turns each response into Items.
2. filters.py drops trade, dosing and how-to posts, blocked channels and off-topic items.
3. store.py keeps items in news_items, glues one story from several sources together.
4. triage.py: a cheap model writes a one-line Russian gist per item, picks a section and
   flags what the code filters missed.
5. rank.py picks the strongest candidates per section; digest.py has the smart model write
   the digest from them and renders it. Links always come from stored URLs, never from a model.

service.py runs steps 1-4 in the collector process; the answerer posts the digest.
"""
