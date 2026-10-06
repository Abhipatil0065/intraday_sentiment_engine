from app.database import init_db, session_scope
from sqlalchemy import text
init_db()
with session_scope() as s:
    rows = s.execute(text("select id,asset,scope,bias,score,confidence,note,contributing_articles,computed_at from sentiment_snapshot order by id desc limit 15")).all()
    for x in rows:
        print(x)
    print("total rows:", s.execute(text("select count(*) from sentiment_snapshot")).scalar())
    for r in s.execute(text("select type,name,tbl_name,sql from sqlite_master where tbl_name='sentiment_snapshot' or sql like '%sentiment_snapshot%'")):
        print(r)
