import glob
import sqlite3

path = [p for p in glob.glob("/var/lib/tmads/*.sqlite3") if "control" in p][0]
conn = sqlite3.connect(path)
conn.execute(
    "UPDATE campaign_product_upcs SET upc='990000000005' "
    "WHERE campaign_id='cmp_1ce8ba06-43cf-4693-9f6c-e558100b8515' "
    "AND upc='990000000001'"
)
conn.execute(
    "UPDATE campaign_product_upcs SET upc='990000000006' "
    "WHERE campaign_id='cmp_86f92cd3-9466-42f6-a642-7bdd5cf692ba' "
    "AND upc='990000000002'"
)
conn.commit()
for row in conn.execute("SELECT campaign_id, upc FROM campaign_product_upcs ORDER BY upc"):
    print(row)
