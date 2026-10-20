"""Does DataGov chat with only the Luccy tool answer the same as (or better than) Luccy alone,
on DataGov's data?

`python -m luccy_qdrant.compare` asks every question of luccy's golden set (121) of luccy itself
(twice, for its own run-to-run noise) and of DataGov chat with only the Luccy tool (eval arm
chat_analyzer_only), scores every answer with luccy's own judge, compares them in luccy page
ids, sets them against luccy's own recorded score on the same set (golden.py), and posts the
result to DataGov's eval runs endpoint.
See runner.py for the flags and the environment.
"""
