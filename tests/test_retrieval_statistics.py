"""Exact local statistics and earliest evidence with synthetic data/mocked APIs."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from storage import Store,epoch,display_time
from deepseek_api import Client


def row(i,stamp,content="普通记录"):
    return {"local_id":i,"shard":"a","sort_seq":i,"create_time":stamp,
        "type":"文本","sender_name":"测试成员","content":content}


class RetrievalStatisticsTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.store=Store(Path(self.temp.name)/"messages.sqlite3")
        groups=[{"username":"chosen","name":"示例群"},{"username":"other","name":"其他聊天"}]
        self.store.set_selection("account",groups,{"chosen"},epoch("2026-10-01"))
        self.chat=Store.chat_id("account","chosen")
        self.first=epoch("2026-10-01")+6
        self.last=epoch("2026-10-15")+18*3600+57*60+48
        self.store.set_scope(self.chat,epoch("2026-10-01"),epoch("2026-10-15",True))
        self.store.put(self.chat,[row(1,self.first,"最早的普通记录")]+[
            row(i,epoch("2026-10-15")+i,"近期的普通记录") for i in range(2,81)]+[row(81,self.last,"最后的普通记录")])
        self.store.put(self.chat,[row(90,epoch("2026-09-30"),"OUTSIDE_DATE_BEFORE"),
            row(91,epoch("2026-10-16"),"OUTSIDE_DATE_AFTER")])
        self.store.put(Store.chat_id("account","other"),[row(1,epoch("2026-09-01"),"OUTSIDE_CHAT")])

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def response(content):
        return {"choices":[{"message":{"content":content},"finish_reason":"stop"}]}

    def answer(self,order="oldest",keywords=None,start=None,end=None,history=None):
        calls=[]
        def transport(payload):
            calls.append(payload)
            if len(calls)==1:
                return self.response(json.dumps({"keywords":keywords or [],"chat_names":[],
                    "start_date":start,"end_date":end,"order":order}))
            return self.response("本地保存的最早消息为 2026-10-01 00:00:06 [M1]。")
        result=Client("fake-only",transport=transport).answer_retrieved(self.store,"最早的消息是哪条？",history=history)
        return result,calls

    def test_newest_sample_is_limited_but_exact_count_and_earliest_time_are_not(self):
        hits=self.store.search([],limit=5)
        self.assertEqual(len(hits["messages"]),5)
        self.assertTrue(hits["has_more"])
        self.assertNotIn(self.first,[m["timestamp"] for m in hits["messages"]])
        stats=hits["statistics"]
        self.assertEqual((stats["count"],stats["earliest_time"],stats["latest_time"]),
            (81,"2026-10-01 00:00:06","2026-10-15 18:57:48"))
        self.assertTrue(stats["is_exact"])
        self.assertFalse(stats["limited_by_message_cap"])
        self.assertEqual({r["timestamp"] for r in hits["boundary_messages"]},{self.first,self.last})
        oldest=self.store.search([],limit=5,order="oldest")
        self.assertEqual(oldest["messages"][0]["timestamp"],self.first)
        self.assertEqual(oldest["boundary_messages"][0]["timestamp"],self.first)

    def test_model_gets_real_earliest_original_and_separate_exact_statistics(self):
        result,calls=self.answer(history=[{"role":"assistant","content":"最早记录大概在最近一天。"}])
        body=json.loads(calls[-1]["messages"][-1]["content"])
        self.assertTrue(body["results_limited"])
        self.assertEqual(body["results_limit_applies_to"],"supplied_messages_only")
        self.assertEqual(body["coverage"][0]["count"],81)
        self.assertTrue(body["coverage"][0]["count_is_exact"])
        self.assertFalse(body["coverage"][0]["statistics_limited"])
        self.assertEqual(body["coverage"][0]["selected_start_date"],"2026-10-01")
        self.assertEqual(body["coverage"][0]["earliest_saved_time"],"2026-10-01 00:00:06")
        self.assertEqual(body["retrieval_statistics"]["count"],81)
        self.assertEqual(body["supplied_messages"][0]["time"],"2026-10-01 00:00:06")
        self.assertIn("最早的普通记录",body["supplied_messages"][0]["text"])
        self.assertIn({"ref":"M1","time":"2026-10-01 00:00:06"},body["retrieval_statistics"]["boundary_references"])
        self.assertEqual(len({r["id"] for r in result["sources"]}),len(result["sources"]))
        for secret in ("OUTSIDE_DATE_BEFORE","OUTSIDE_DATE_AFTER","OUTSIDE_CHAT"):
            self.assertNotIn(secret,json.dumps(calls))
        self.assertIn("不代表微信全部历史",calls[-1]["messages"][0]["content"])

    def test_earliest_witness_is_still_supplied_when_planner_uses_newest(self):
        _,calls=self.answer(order="newest")
        body=json.loads(calls[-1]["messages"][-1]["content"])
        self.assertEqual(body["supplied_messages"][0]["time"],display_time(self.last))
        self.assertIn(display_time(self.first),[m["time"] for m in body["supplied_messages"]])

    def test_keyword_statistics_cover_all_matches_and_respect_narrower_dates(self):
        self.store.put(self.chat,[row(100,epoch("2026-10-10")+60,"预算表第一次提及"),
            row(101,epoch("2026-10-12")+60,"预算表再次提及"),row(102,self.last+1,"无关消息")])
        first=self.store.search(["预算表"],limit=1,order="oldest")
        self.assertEqual(first["statistics"]["count"],2)
        self.assertEqual(first["statistics"]["earliest_time"],"2026-10-10 00:01:00")
        self.assertEqual(first["messages"][0]["content"],"预算表第一次提及")
        narrowed=self.store.search(["预算"],start_date="2026-10-11",end_date="2026-10-12",limit=1,order="oldest")
        self.assertEqual(narrowed["statistics"]["count"],1)
        self.assertEqual(narrowed["statistics"]["earliest_time"],"2026-10-12 00:01:00")
        result,calls=self.answer(keywords=["预算"],start="2026-10-11",end="2026-10-12")
        self.assertTrue(all("2026-10-11"<=m["time"][:10]<="2026-10-12" for m in result["sources"]))
        body=json.loads(calls[-1]["messages"][-1]["content"])
        self.assertEqual(body["retrieval_statistics"]["count"],1)
        self.assertEqual(body["coverage"][0]["count"],84)
        self.assertNotIn("预算表第一次提及",json.dumps(body,ensure_ascii=False))

    def test_empty_match_has_exact_zero_without_exposing_out_of_scope_boundaries(self):
        hits=self.store.search(["OUTSIDE_DATE_BEFORE"],order="oldest")
        self.assertEqual(hits["statistics"]["count"],0)
        self.assertIsNone(hits["statistics"]["earliest_time"])
        self.assertEqual(hits["boundary_messages"],[])
        self.assertFalse(hits["has_more"])
        result,calls=self.answer(keywords=["完全不存在的话题"])
        self.assertEqual(result["sources"],[])
        self.assertEqual(result["retrieval_statistics"]["count"],0)
        self.assertEqual(len(calls),1)

    def test_timestamp_ties_keep_same_boundary_message_as_ordered_results(self):
        stamp=epoch("2026-10-11")+300
        self.store.put(self.chat,[row(i,stamp,"同秒事件") for i in range(200,204)])
        for order in ("oldest","newest"):
            hits=self.store.search(["同秒"],order=order,limit=1)
            self.assertEqual(hits["statistics"]["count"],4)
            self.assertEqual(hits["messages"][0]["id"],hits["boundary_messages"][0]["id"])
            self.assertEqual(hits["messages"][0]["seq"],200 if order=="oldest" else 203)

    def test_sort_options_are_validated_and_old_plans_remain_compatible(self):
        plan={"keywords":[],"chat_names":[],"start_date":None,"end_date":None}
        client=Client("fake-only",transport=lambda _:self.response(json.dumps(plan)))
        self.assertEqual(client.plan("查询",["示例群"])["order"],"newest")
        for invalid in ("ASC; DROP TABLE messages",None,["oldest"],1):
            plan["order"]=invalid
            with self.assertRaises(RuntimeError): client.plan("查询",["示例群"])
            with self.assertRaises(ValueError): self.store.search([],order=invalid)


if __name__=="__main__":
    unittest.main()
