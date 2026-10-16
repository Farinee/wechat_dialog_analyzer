import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from deepseek_api import Client
from storage import Store,epoch
from rag_index import RagIndex,scope_key,chat_key,digest
from rag_pipeline import answer_rag
from history_memory import prepare_history


class Counter:
    def count(self,text):
        return len(text)


def row(i,text="预算项目已确认",stamp=None):
    return {"local_id":i,"shard":"test","sort_seq":i,"create_time":epoch("2026-10-10")+i if stamp is None else stamp,
        "sender_name":"测试成员","type":"文本","content":text}


class RagTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.path=Path(self.temp.name)
        self.store=Store(self.path/"messages.sqlite3")
        self.groups=[{"username":"chosen","name":"所选聊天"},{"username":"other","name":"其他聊天"}]
        self.store.set_selection("account",self.groups,{"chosen"},epoch("2026-10-01"))
        self.chat=Store.chat_id("account","chosen")
        self.scope=(self.chat,epoch("2026-10-10"),epoch("2026-10-11",True))
        self.store.set_scope(*self.scope)
        self.index=RagIndex(self.path/"rag.sqlite3")
        self.counter=Counter()
        self.calls=[]
        self.keywords=["预算"]
        self.narrow=None
        self.planner_hook=None
        self.fail_summary=False
        def transport(payload):
            self.calls.append(payload)
            system=payload["messages"][0]["content"]
            if "你负责压缩" in system:
                content="invalid" if self.fail_summary else json.dumps({"summary":"用户讨论预算项目，已经确认部分金额，负责人仍待核实。旧答案标注 [M1]。"},ensure_ascii=False)
            elif "查询规划器" in system:
                if self.planner_hook:
                    self.planner_hook()
                content=json.dumps({"keywords":self.keywords,"chat_names":[],"start_date":self.narrow,
                    "end_date":self.narrow,"order":"oldest"})
            else:
                content="预算项目已经确认，详见 [M1]。"
            reasoning="辅助调用思考" if "查询规划器" in system or "你负责压缩" in system else "正式分析的思考"
            return {"choices":[{"message":{"content":content,"reasoning_content":reasoning},"finish_reason":"stop"}]}
        self.client=Client("fake-test-key",transport=transport)

    def tearDown(self):
        self.temp.cleanup()

    def sync(self,**kwargs):
        return self.index.sync(self.store,self.scope,self.counter,model="test",**kwargs)

    def answer(self,**kwargs):
        return answer_rag(self.client,self.store,"预算项目怎么样了？",counter=self.counter,index=self.index,**kwargs)

    def test_incremental_reuse_late_insert_content_change_and_restart(self):
        self.store.put(self.chat,[row(1),row(2,stamp=epoch("2026-10-10")+4000)])
        first=self.sync()
        self.assertEqual(first["changed_buckets"],2)
        unchanged=self.sync()
        self.assertEqual(unchanged["changed_buckets"],0)
        self.assertEqual(unchanged["generation"],first["generation"])
        self.store.put(self.chat,[row(3,stamp=epoch("2026-10-10")+20)])
        updated=self.sync()
        self.assertEqual((updated["changed_buckets"],updated["reused_buckets"]),(1,1))
        self.store.put(self.chat,[row(1,"预算更正为500万元")])
        self.assertEqual(self.sync()["changed_buckets"],1)
        reopened=RagIndex(self.path/"rag.sqlite3")
        result=reopened.retrieve(self.scope,{"keywords":["预算更正"],"order":"oldest"})
        self.assertEqual(result["statistics"]["count"],1)
        self.assertIn("500万元",json.dumps(result,ensure_ascii=False))

    def test_date_subsets_overlaps_and_restarts_share_one_copy(self):
        day=epoch("2026-10-10")
        self.store.put(self.chat,[row(1,stamp=day+10),row(2,stamp=day+86410),row(3,stamp=day+172810)])
        first=self.sync()
        narrow=(self.chat,day+86400,day+172800)
        reused=self.index.sync(self.store,narrow,self.counter,model="test")
        self.assertEqual((reused["changed_buckets"],reused["reused_buckets"]),(0,1))
        self.assertEqual(first["generation"],reused["generation"])
        result=self.index.retrieve(narrow,{"keywords":[]})
        self.assertEqual(result["coverage"]["count"],1)
        self.assertEqual(result["coverage"]["selected_start_date"],"2026-10-11")
        self.assertEqual(result["coverage"]["earliest"],day+86410)
        overlap=(self.chat,day+86400,day+259200)
        expanded=self.index.sync(self.store,overlap,self.counter,model="test")
        self.assertEqual((expanded["changed_buckets"],expanded["reused_buckets"]),(1,1))
        with self.index.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0],3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scopes").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM indexed_ranges").fetchone()[0],1)
        reopened=RagIndex(self.index.path)
        self.assertEqual(reopened.retrieve(self.scope,{"keywords":[]})["coverage"]["count"],2)
        self.assertEqual(self.sync()["changed_buckets"],0)

    def test_partial_buckets_keep_outside_records_and_exact_range_completeness(self):
        day=epoch("2026-10-10")
        self.store.put(self.chat,[row(1,stamp=day+10),row(2,stamp=day+30),row(3,stamp=day+90)])
        partial=(self.chat,day+20,day+60)
        self.index.sync(self.store,partial,self.counter,model="test")
        with self.assertRaisesRegex(RuntimeError,"尚未建立完整"):
            self.index.retrieve(self.scope,{"keywords":[]})
        self.assertEqual(self.sync()["changed_buckets"],1)
        self.assertEqual(self.index.sync(self.store,partial,self.counter,model="test")["changed_buckets"],0)
        self.store.put(self.chat,[row(2,"预算修正",stamp=day+30)])
        self.assertEqual(self.index.sync(self.store,partial,self.counter,model="test")["changed_buckets"],1)
        with self.store.connect() as conn:
            conn.execute("DELETE FROM messages WHERE timestamp=?",(day+30,))
        self.index.sync(self.store,partial,self.counter,model="test")
        self.assertEqual(self.index.retrieve(partial,{"keywords":[]})["coverage"]["count"],0)
        self.assertEqual(self.index.retrieve(self.scope,{"keywords":[]})["coverage"]["count"],2)

    def test_narrow_deletions_preserve_other_days_and_missing_ranges_fail_closed(self):
        day=epoch("2026-10-10")
        self.store.put(self.chat,[row(1,stamp=day+10),row(2,stamp=day+86410)])
        self.sync()
        with self.store.connect() as conn:
            conn.execute("DELETE FROM messages WHERE timestamp=?",(day+86410,))
        narrow=(self.chat,day+86400,day+172800)
        self.assertEqual(self.index.sync(self.store,narrow,self.counter,model="test")["removed_buckets"],1)
        self.assertEqual(self.index.retrieve(self.scope,{"keywords":[]})["coverage"]["count"],1)
        gap=(self.chat,day+259200,day+345600)
        self.index.sync(self.store,gap,self.counter,model="test")
        with self.assertRaisesRegex(RuntimeError,"尚未建立完整"):
            self.index.retrieve((self.chat,day,day+345600),{"keywords":[]})
        bridge=(self.chat,day+172800,day+259200)
        self.index.sync(self.store,bridge,self.counter,model="test")
        self.assertEqual(self.index.retrieve((self.chat,day,day+345600),{"keywords":[]})["coverage"]["count"],1)

    def test_shared_database_keeps_chat_and_account_boundaries(self):
        self.store.put(self.chat,[row(1,"预算 CHOSEN")])
        self.sync()
        for account,username in (("account","other"),("different-account","chosen")):
            self.store.set_selection(account,self.groups,{username},epoch("2026-10-01"))
            chat=Store.chat_id(account,username)
            self.store.put(chat,[row(1,"预算 PRIVATE "+account)])
            self.index.sync(self.store,(chat,*self.scope[1:]),self.counter,model="test")
        result=self.index.retrieve(self.scope,{"keywords":["预算"]})
        self.assertEqual(result["coverage"]["count"],1)
        self.assertNotIn("PRIVATE",json.dumps(result,ensure_ascii=False))
        with self.index.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scopes").fetchone()[0],3)

    def legacy_index(self,ranges):
        # Produce real old-format date namespaces without relying on live data
        # or retaining a second implementation of the indexer.
        with self.index.connect() as conn:
            for table in ("chunks","records","buckets","scopes","indexed_ranges","rag_meta"):
                conn.execute(f"DELETE FROM {table}")
        for i,scope in enumerate(ranges):
            fixture=RagIndex(self.path/f"legacy-{i}.sqlite3")
            fixture.sync(self.store,scope,self.counter,model="test")
            with fixture.connect() as original,self.index.connect() as conn:
                for table in ("scopes","buckets","records","chunks"):
                    for row_data in original.execute(f"SELECT * FROM {table}"):
                        value=dict(row_data)
                        if table=="scopes":
                            value["id"]=scope_key(scope)
                        else:
                            value["scope"]=scope_key(scope)
                        if table=="chunks":
                            value["id"]=digest([scope_key(scope),value["id"]])
                        conn.execute(f"INSERT INTO {table} VALUES("+",".join("?" for _ in value)+")",list(value.values()))

    def test_legacy_migration_reuses_existing_chunks_and_preserves_summary(self):
        self.store.put(self.chat,[row(1)])
        self.legacy_index([self.scope])
        with self.index.connect() as conn:
            conn.execute("INSERT INTO memories VALUES(?,?,?,?,?)",(scope_key(self.scope),"saved-session",4,"prefix-hash","历史摘要"))
            chunk_ids=[r[0] for r in conn.execute("SELECT id FROM chunks")]
        class NoTokenization:
            def count(self,text):
                raise AssertionError("Unchanged migrated chunks must not be tokenized again")
        result=self.index.sync(self.store,self.scope,NoTokenization(),model="test")
        self.assertEqual((result["changed_buckets"],result["reused_buckets"]),(0,1))
        with self.index.connect() as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT id FROM chunks")],chunk_ids)
            self.assertEqual(conn.execute("SELECT scope,summary FROM memories").fetchone()[:],(scope_key(self.scope),"历史摘要"))
            self.assertEqual(conn.execute("SELECT id FROM scopes").fetchone()[0],chat_key(self.chat))
        self.assertEqual(self.index.retrieve(self.scope,{"keywords":["预算项目"]})["statistics"]["count"],1)

    def test_legacy_overlapping_date_indices_merge_duplicates_transactionally(self):
        day=epoch("2026-10-10")
        self.store.put(self.chat,[row(1),row(2,stamp=day+86410),row(3,stamp=day+172810)])
        overlap=(self.chat,day+86400,day+259200)
        self.legacy_index([self.scope,overlap])
        self.sync()
        self.assertEqual(self.index.sync(self.store,overlap,self.counter,model="test")["changed_buckets"],0)
        with self.index.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scopes").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0],3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM buckets").fetchone()[0],3)
        self.assertEqual(self.index.retrieve(overlap,{"keywords":["预算项目"]})["statistics"]["count"],2)

    def test_cancelled_migration_retains_legacy_namespace_and_retries(self):
        self.store.put(self.chat,[row(1)])
        self.legacy_index([self.scope])
        stop=threading.Event()
        with self.assertRaisesRegex(RuntimeError,"已停止"):
            self.sync(stop=stop,progress=lambda _:stop.set())
        with self.index.connect() as conn:
            self.assertEqual(conn.execute("SELECT id FROM scopes").fetchone()[0],scope_key(self.scope))
            self.assertIsNone(conn.execute("SELECT 1 FROM rag_meta").fetchone())
        self.assertEqual(self.sync()["changed_buckets"],0)

    def test_partial_legacy_buckets_merge_and_recheck_deleted_source_rows(self):
        day=epoch("2026-10-10")
        self.store.put(self.chat,[row(1,stamp=day+10),row(2,stamp=day+30),row(3,stamp=day+90)])
        self.legacy_index([(self.chat,day,day+60),(self.chat,day+20,day+120)])
        with self.store.connect() as conn:
            conn.execute("DELETE FROM messages WHERE timestamp=?",(day+30,))
        self.sync()
        result=self.index.retrieve(self.scope,{"keywords":["预算项目"]})
        self.assertEqual(result["coverage"]["count"],2)
        self.assertEqual(result["statistics"]["count"],2)
        with self.index.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0],2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM buckets").fetchone()[0],1)

    def test_narrow_answer_never_sends_other_cached_dates_or_counts(self):
        day=epoch("2026-10-10")
        self.store.put(self.chat,[row(1,"预算 OLD_PRIVATE"),row(2,"预算 范围内",stamp=day+86410)])
        self.index.sync(self.store,self.scope,self.counter,model=self.client.model)
        self.store.set_scope(self.chat,day+86400,day+172800)
        result=self.answer()
        self.assertEqual(result["coverage"][0]["count"],1)
        self.assertEqual(result["retrieval_statistics"]["count"],1)
        self.assertEqual(result["analysis_coverage"]["index"]["changed_buckets"],0)
        self.assertNotIn("OLD_PRIVATE",json.dumps(self.calls,ensure_ascii=False))

    def test_sharing_source_index_does_not_share_memories_across_dates(self):
        self.store.put(self.chat,[row(1)])
        self.sync()
        turns=[{"turn_id":str(i),"question":"预算项目","answer":"待核实"} for i in range(6)]
        prepare_history(self.index,self.scope,"same-session",turns,"预算",self.client,self.counter)
        self.calls.clear()
        narrow=(self.chat,self.scope[1],self.scope[1]+86400)
        self.index.sync(self.store,narrow,self.counter,model="test")
        prepare_history(self.index,narrow,"same-session",turns,"预算",self.client,self.counter)
        self.assertTrue(self.calls)
        with self.index.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scopes").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],2)

    def test_interrupted_update_rolls_back_published_index_and_retries(self):
        self.store.put(self.chat,[row(1)])
        self.sync()
        self.store.put(self.chat,[row(2,"预算新增")])
        stop=threading.Event()
        with self.assertRaises(RuntimeError):
            self.sync(stop=stop,progress=lambda _:stop.set())
        result=self.index.retrieve(self.scope,{"keywords":[]})
        self.assertEqual(result["coverage"]["count"],1)
        self.assertEqual(self.sync()["changed_buckets"],1)
        self.assertEqual(self.index.retrieve(self.scope,{"keywords":[]})["coverage"]["count"],2)

    def test_scope_is_frozen_and_narrower_question_dates_are_enforced(self):
        self.store.put(self.chat,[row(1,"预算 OUTSIDE_DATE"),row(2,"预算 日期内",stamp=epoch("2026-10-11"))])
        other=Store.chat_id("account","other")
        self.store.put(other,[row(1,"预算 OTHER_CHAT")])
        self.narrow="2026-10-11"
        self.planner_hook=lambda:self.store.set_scope(other,epoch("2026-10-10"),epoch("2026-10-11",True))
        answer=self.answer()
        outgoing=json.dumps(self.calls,ensure_ascii=False)
        self.assertNotIn("OUTSIDE_DATE",outgoing)
        self.assertNotIn("OTHER_CHAT",outgoing)
        self.assertEqual(answer["retrieval_statistics"]["count"],1)
        self.assertTrue(all(s["time"].startswith("2026-10-11") for s in answer["sources"]))

    def test_all_matches_are_sent_even_with_an_obsolete_small_rag_budget(self):
        self.store.put(self.chat,[row(i,"预算项目"+"必要说明。"*40) for i in range(1,151)])
        result=self.answer(budget=4096)
        self.assertEqual(result["retrieval_statistics"]["count"],150)
        self.assertEqual(result["coverage"][0]["count"],150)
        self.assertEqual(len(result["sources"]),150)
        self.assertGreater(result["analysis_coverage"]["estimated_input_tokens"],16000)
        self.assertEqual(result["analysis_coverage"]["omitted_matched_messages"],0)
        self.assertTrue(result["analysis_coverage"]["all_matches_included"])
        self.assertFalse(result["has_more"])
        body=json.loads(self.calls[-1]["messages"][-1]["content"])
        self.assertEqual(len(body["supplied_messages"]),150)
        self.assertFalse(body["results_limited"])
        self.assertEqual([s["text"] for s in body["supplied_messages"]],[s["text"] for s in result["sources"]])
        self.assertEqual(self.store.stats()[0]["count"],150)

    def test_long_matching_message_is_sent_complete_and_archive_is_retained(self):
        text="前面的普通内容。"*1500+"预算项目最后确定为500万元。"+"后续内容。"*200
        self.store.put(self.chat,[row(1,text)])
        result=self.answer()
        self.assertIn("500万元",result["sources"][0]["text"])
        self.assertEqual(result["sources"][0]["text"],text)
        self.assertEqual(json.loads(self.calls[-1]["messages"][-1]["content"])["supplied_messages"][0]["text"],text)
        self.assertEqual(result["analysis_coverage"]["excerpted_messages"],0)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT content FROM messages").fetchone()[0],text)

    def test_history_summary_is_incremental_cached_and_scope_session_isolated(self):
        turns=[{"turn_id":str(i),"question":f"第{i}轮预算项目？","answer":"预算负责人还未确认 [M1]。"} for i in range(8)]
        history,info=prepare_history(self.index,self.scope,"session",turns,"预算负责人",self.client,self.counter)
        self.assertEqual(info["summarized_turns"],5)
        self.assertGreater(info["summary_calls"],0)
        self.assertNotIn("[M1]",json.dumps(history,ensure_ascii=False))
        self.calls.clear()
        _,cached=prepare_history(self.index,self.scope,"session",turns,"预算负责人",self.client,self.counter)
        self.assertTrue(cached["summary_cached"])
        self.assertEqual(self.calls,[])
        turns.append({"turn_id":"8","question":"最新问题","answer":"最新答案"})
        prepare_history(self.index,self.scope,"session",turns,"预算负责人",self.client,self.counter)
        additional=json.loads(self.calls[-1]["messages"][-1]["content"])["additional_history"]
        self.assertTrue(all(part["turn"]==6 for part in additional))
        self.calls.clear()
        prepare_history(self.index,self.scope,"different-session",turns,"预算负责人",self.client,self.counter)
        self.assertTrue(self.calls)

    def test_invalid_summary_preserves_prior_cache_and_original_turns(self):
        turns=[{"turn_id":str(i),"question":"预算","answer":str(i)} for i in range(6)]
        prepare_history(self.index,self.scope,"session",turns,"预算",self.client,self.counter)
        with self.index.connect() as conn:
            before=dict(conn.execute("SELECT * FROM memories").fetchone())
        turns.append({"turn_id":"6","question":"预算","answer":"新的回答"})
        self.fail_summary=True
        with self.assertRaisesRegex(RuntimeError,"历史摘要格式"):
            prepare_history(self.index,self.scope,"session",turns,"预算",self.client,self.counter)
        with self.index.connect() as conn:
            after=dict(conn.execute("SELECT * FROM memories").fetchone())
        self.assertEqual(before,after)
        self.assertEqual(len(turns),7)

    def test_no_hits_do_not_make_answer_request_and_no_source_access(self):
        self.store.put(self.chat,[row(1,"普通内容")])
        self.keywords=["绝对没有这个词"]
        result=self.answer()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(result["sources"],[])
        self.assertIn("没有找到",result["answer"])

    def test_saved_materials_are_searchable_deduplicated_and_reanalysis_invalidates_only_index(self):
        import io
        from PIL import Image
        buffer=io.BytesIO()
        Image.new("RGB",(16,16),"blue").save(buffer,format="PNG")
        data=buffer.getvalue()
        rows=[dict(row(i,"[图片]"),type="图片") for i in (1,2)]
        self.store.put(self.chat,rows)
        asset=self.store.media.register(data,data,"image/png")
        with self.store.connect() as conn:
            ids=[r[0] for r in conn.execute("SELECT id FROM messages ORDER BY seq")]
        for uid in ids:
            self.store.media.link(uid,asset,"image")
        self.store.media.save_analysis(asset["hash"],{"summary":"图片说明。"*300+"预算120万元。"+"尾部说明。"*300},"mock","v1")
        with self.store.connect() as conn:
            complete_description=conn.execute("SELECT text FROM media_analyses ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        result=self.answer()
        self.assertEqual(result["retrieval_statistics"]["count"],2)
        self.assertEqual(len(result["assets"]),1)
        self.assertEqual(len(result["sources"]),2)
        self.assertEqual(result["assets"][asset["hash"]]["description"],complete_description)
        sent=json.loads(self.calls[-1]["messages"][-1]["content"])
        self.assertEqual(sent["supplied_assets"][asset["hash"]]["description"],complete_description)
        self.assertEqual(len(self.calls),2) # planner + text answer, no VLM
        self.store.media.save_analysis(asset["hash"],{"summary":"预算130万元"},"mock","v2")
        updated=self.index.sync(self.store,self.scope,self.counter,model=self.client.model)
        self.assertEqual(updated["changed_buckets"],1)
        self.assertEqual(self.index.retrieve(self.scope,{"keywords":["120万元"]})["statistics"]["count"],0)
        self.assertEqual(self.index.retrieve(self.scope,{"keywords":["130万元"]})["statistics"]["count"],2)

    def test_concurrent_index_publish_is_detected_before_answer(self):
        self.store.put(self.chat,[row(1)])
        def mutate():
            self.store.put(self.chat,[row(2,"预算新增")])
            self.index.sync(self.store,self.scope,self.counter,model=self.client.model)
        self.planner_hook=mutate
        with self.assertRaisesRegex(RuntimeError,"另一任务更新"):
            self.answer()
        self.assertEqual(len(self.calls),1)

    def test_cancel_before_answer_prevents_final_request(self):
        self.store.put(self.chat,[row(1)])
        stop=threading.Event()
        with self.assertRaises(RuntimeError):
            self.answer(stop=stop,notice=lambda _:stop.set())
        self.assertEqual(len(self.calls),1)

    def test_api_and_ui_rag_path_preserve_session_and_reuse_index(self):
        import tkinter as tk
        import time
        from app import App
        root=tk.Tk()
        root.withdraw()
        app=App(root,self.path,auto=False,source_factory=lambda *a,**k:self.fail("RAG must never reopen WeChat"))
        try:
            app.account,app.groups,app.loaded="account",self.groups,True
            app.selected_names={"chosen"}
            app.since.set("2026-10-10")
            app.until.set("2026-10-11")
            app.config.update(context_mode="rag",rag_input_tokens=16000)
            app.make_client=lambda:self.client
            app.store.put(self.chat,[row(1)])
            for question in ("预算项目怎么样？","后来呢？"):
                app.question.delete("1.0","end")
                app.question.insert("1.0",question)
                app.ask()
                deadline=time.monotonic()+10
                while app.busy and time.monotonic()<deadline:
                    root.update()
                    time.sleep(.01)
                self.assertFalse(app.busy)
            self.assertEqual(len(app.conversation_turns),2)
            self.assertEqual(app.conversation_turns[-1]["reasoning_content"],"正式分析的思考")
            self.assertEqual(app.conversation_turns[-1]["plan"]["mode"],"rag")
            self.assertEqual(app.conversation_turns[-1]["analysis_coverage"]["index"]["changed_buckets"],0)
            self.assertIn("RAG 压缩",app.conversation_turns[-1]["source_text"])
        finally:
            for timer in root.tk.call("after","info"):
                root.after_cancel(timer)
            root.destroy()

    def test_global_capacity_validation_and_oversized_prompts_fail_before_analysis(self):
        for invalid in (True,4095,800001,1.5,"abc"):
            with self.assertRaises(ValueError):
                self.answer(max_input_tokens=invalid)
        self.store.put(self.chat,[row(1)])
        self.client.system_prompt="冗长提示词"*2000
        with self.assertRaises(RuntimeError):
            self.answer(max_input_tokens=4096)
        self.assertLessEqual(len(self.calls),1) # planner may run, answer must not

    def test_matches_above_160000_tokens_and_24_chunks_are_all_sent(self):
        day=epoch("2026-10-10")
        texts={i:"预算项目"+str(i)+"。"*600 for i in range(1,321)}
        self.store.put(self.chat,[row(i,texts[i],stamp=day+i*200) for i in texts])
        result=self.answer(budget=160000)
        self.assertEqual(result["retrieval_statistics"]["count"],320)
        self.assertEqual(len(result["sources"]),320)
        self.assertGreater(result["analysis_coverage"]["estimated_input_tokens"],160000)
        self.assertEqual(result["analysis_coverage"]["omitted_matched_messages"],0)
        self.assertEqual([source["text"] for source in result["sources"]],list(texts.values()))
        self.assertEqual(len(json.loads(self.calls[-1]["messages"][-1]["content"])["supplied_messages"]),320)
        with self.index.connect() as conn:
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],24)

    def test_keyword_or_returns_all_matches_and_empty_keywords_return_all_scoped_rows(self):
        self.store.put(self.chat,[row(1,"普通前文"),row(2,"预算项目"),row(3,"普通后文"),row(4,"费用说明")])
        self.keywords=["预算","费用"]
        result=self.answer()
        self.assertEqual([source["text"] for source in result["sources"]],["普通前文","预算项目","普通后文","费用说明"])
        self.assertEqual([source["text"] for source in result["sources"] if source["retrieval_role"]=="match"],["预算项目","费用说明"])
        self.assertEqual(result["analysis_coverage"]["context_messages"],2)
        self.assertTrue(result["analysis_coverage"]["complete"])
        self.keywords=[]
        result=self.answer()
        self.assertEqual(result["retrieval_statistics"]["count"],4)
        self.assertEqual(len(result["sources"]),4)
        self.assertTrue(result["analysis_coverage"]["complete"])

    def test_matches_include_questions_pronouns_negation_and_corrections_as_one_passage(self):
        texts=["这次活动到底花多少钱？","预算暂定500元。","不对，这个数字不准确。","刚核实过，最后是300元。","好的，按这个数执行。"]
        self.store.put(self.chat,[dict(row(i,text),sender_name=f"成员{i}") for i,text in enumerate(texts,1)])
        result=self.answer()
        self.assertEqual(result["retrieval_statistics"]["count"],1)
        info=result["analysis_coverage"]
        self.assertEqual((info["included_matched_messages"],info["context_messages"],info["included_messages"]),(1,4,5))
        self.assertEqual([source["text"] for source in result["sources"]],texts)
        self.assertEqual([source["sender"] for source in result["sources"]],[f"成员{i}" for i in range(1,6)])
        body=json.loads(self.calls[-1]["messages"][-1]["content"])
        self.assertEqual(body["conversation_passages"],[{"ref":"C1","start_ref":"M1","end_ref":"M5","message_count":5,"matched_refs":["M2"]}])
        self.assertEqual([source["retrieval_role"] for source in body["supplied_messages"]],["context","match","context","context","context"])
        self.assertTrue(body["all_matches_included"])
        self.assertEqual(self.store.stats()[0]["count"],5)
        # An unchanged source index is still reused after enabling windows.
        self.assertEqual(self.answer()["analysis_coverage"]["index"]["changed_buckets"],0)

    def test_overlapping_windows_merge_deduplicate_and_keep_full_neighbor_text(self):
        texts={i:("预算项目" if i in (35,40) else f"第{i}条上下文") for i in range(1,81)}
        texts[20]+="完整的前后说明。"*1000
        self.store.put(self.chat,[row(i,texts[i]) for i in texts])
        result=self.answer()
        self.assertEqual([source["text"] for source in result["sources"]],[texts[i] for i in range(15,61)])
        self.assertEqual(len({source["id"] for source in result["sources"]}),46)
        self.assertEqual(result["analysis_coverage"]["passage_count"],1)
        self.assertEqual(result["analysis_coverage"]["included_matched_messages"],2)
        self.assertEqual(result["analysis_coverage"]["context_messages"],44)
        body=json.loads(self.calls[-1]["messages"][-1]["content"])
        self.assertEqual(body["conversation_passages"][0]["matched_refs"],["M21","M26"])
        self.assertEqual(body["supplied_messages"][5]["text"],texts[20])

    def test_long_silent_gap_stops_neighbors_and_separates_passages(self):
        day=epoch("2026-10-10")
        self.store.put(self.chat,[row(1,"早先无关内容",stamp=day+1),row(2,"前文问题",stamp=day+3600),
            row(3,"预算暂定",stamp=day+3601),row(4,"后来更正为300",stamp=day+3602),
            row(5,"预算另一个话题",stamp=day+7200),row(6,"新的后文",stamp=day+7201),
            row(7,"之后无关内容",stamp=day+10800)])
        result=self.answer()
        self.assertEqual([source["text"] for source in result["sources"]],["前文问题","预算暂定","后来更正为300","预算另一个话题","新的后文"])
        self.assertEqual(result["analysis_coverage"]["passage_count"],2)
        self.assertEqual([source["passage_ref"] for source in result["sources"]],["C1"]*3+["C2"]*2)
        self.assertEqual(result["analysis_coverage"]["omitted_messages"],2)
        self.assertFalse(result["analysis_coverage"]["complete"])

    def test_time_window_and_same_timestamp_order_are_bounded_and_deterministic(self):
        day=epoch("2026-10-10")
        self.store.put(self.chat,[row(i,"预算" if i==31 else f"持续讨论{i}",stamp=day+i*60) for i in range(1,62)])
        result=self.answer()
        self.assertEqual(len(result["sources"]),31)
        self.assertEqual([source["text"] for source in result["sources"]],["预算" if i==31 else f"持续讨论{i}" for i in range(16,47)])
        # Identical timestamps still use seq/id for a stable conversation order.
        self.store.put(self.chat,[row(i,"预算" if i==31 else f"并列消息{i}",stamp=day+100) for i in range(1,62)])
        result=self.answer()
        self.assertEqual([source["text"] for source in result["sources"]],["预算" if i==31 else f"并列消息{i}" for i in range(11,52)])

    def test_context_neighbors_respect_narrow_question_dates_and_other_cached_chats(self):
        day=epoch("2026-10-11")
        self.store.put(self.chat,[row(1,"禁止发送的前一天上下文",stamp=day-1),row(2,"预算暂定",stamp=day+1),
            row(3,"当日后续更正",stamp=day+2),row(4,"禁止发送的后一天下文",stamp=day+86400)])
        self.index.sync(self.store,(self.chat,epoch("2026-10-10"),epoch("2026-10-12",True)),self.counter,model=self.client.model)
        self.store.set_selection("account",self.groups,{"chosen","other"},epoch("2026-10-01"))
        other=Store.chat_id("account","other")
        self.store.put(other,[row(1,"禁止发送的其他聊天上下文",stamp=day+1)])
        self.index.sync(self.store,(other,*self.scope[1:]),self.counter,model=self.client.model)
        self.narrow="2026-10-11"
        result=self.answer()
        self.assertEqual([source["text"] for source in result["sources"]],["预算暂定","当日后续更正"])
        self.assertEqual(result["retrieval_statistics"]["count"],1)
        self.assertNotIn("禁止发送",json.dumps(self.calls,ensure_ascii=False))

    def test_context_window_can_reach_zero_but_all_matches_and_warning_are_retained(self):
        self.store.put(self.chat,[row(1,"必须保留的长篇前文。"*1500),row(2,"预算确认"),row(3,"简短后文")])
        result=self.answer(max_input_tokens=4096)
        self.assertEqual([source["text"] for source in result["sources"]],["预算确认"])
        info=result["analysis_coverage"]
        self.assertEqual(info["context_policy"]["messages_before"],0)
        self.assertEqual(info["omitted_context_messages"],2)
        self.assertTrue(info["context_window_reduced"])
        self.assertIn("缩至各0条",result["answer"])
        self.assertEqual(len(self.calls),2) # Planner and exactly one final request.
        self.assertEqual(self.store.stats()[0]["count"],3)

    def test_overflow_narrows_neighbors_from_one_snapshot_preserving_every_match(self):
        texts={i:("预算项目" if i%20==10 else f"上下文{i}："+"必要说明。"*30) for i in range(1,501)}
        self.store.put(self.chat,[row(i,texts[i]) for i in texts])
        changed=[]
        def progress(text):
            if "按容量收紧" in text and not changed:
                changed.append(True)
                self.store.put(self.chat,[row(10,"预算原文被新任务改动"),row(501,"预算刚刚新增")])
                self.index.sync(self.store,self.scope,self.counter,model=self.client.model)
        result=answer_rag(self.client,self.store,"预算项目怎么样？",progress,counter=self.counter,index=self.index,max_input_tokens=60000)
        info=result["analysis_coverage"]
        self.assertTrue(info["context_window_reduced"])
        self.assertGreater(info["context_policy"]["messages_before"],0)
        self.assertLess(info["context_policy"]["messages_before"],20)
        self.assertGreater(info["context_messages"],0)
        self.assertLess(info["context_messages"],info["original_context_messages"])
        self.assertLessEqual(info["estimated_input_tokens"],60000)
        self.assertEqual(info["included_matched_messages"],25)
        self.assertEqual(info["omitted_matched_messages"],0)
        self.assertEqual([source["text"] for source in result["sources"] if source["retrieval_role"]=="match"],[texts[i] for i in texts if i%20==10])
        body=json.loads(self.calls[-1]["messages"][-1]["content"])
        self.assertTrue(body["context_reduction"]["reduced"])
        self.assertEqual(body["context_policy"],info["context_policy"])
        self.assertNotIn("被新任务改动",json.dumps(body,ensure_ascii=False))
        self.assertNotIn("刚刚新增",json.dumps(body,ensure_ascii=False))
        self.assertIn("容量调整",result["answer"])
        self.assertEqual(len(self.calls),2)

    def test_history_is_reduced_before_supplementary_context(self):
        self.store.put(self.chat,[row(1,"预算项目"),row(2,"后面的解释")])
        history=[{"role":"user","content":"之前的问题"*180},{"role":"assistant","content":"之前的答案"*180}]
        result=self.answer(history=history,max_input_tokens=4096)
        self.assertGreater(result["analysis_coverage"]["history"]["additional_omitted_parts"],0)
        self.assertFalse(result["analysis_coverage"]["context_window_reduced"])
        self.assertEqual(len(result["sources"]),2)

    def test_default_byte_ceiling_allows_large_text_when_token_budget_fits(self):
        class ByteCounter:
            def count(self,text): return (len(text)+9)//10
        self.counter=ByteCounter()
        self.store.put(self.chat,[row(i,"预算"+"x"*750000) for i in (1,2,3)])
        result=self.answer()
        info=result["analysis_coverage"]
        self.assertGreater(info["request_bytes"],2*1024*1024)
        self.assertEqual(info["request_byte_limit"],32*1024*1024)
        self.assertEqual(len(result["sources"]),3)
        self.assertEqual(len(self.calls),2)

    def test_token_limit_error_is_distinct_from_request_byte_limit(self):
        self.store.put(self.chat,[row(1,"预算"+"必要原文"*10000)])
        with self.assertRaisesRegex(RuntimeError,"输入 token 超限") as caught:
            self.answer(max_input_tokens=4096)
        self.assertIn("4,096",str(caught.exception))
        self.assertNotIn("本地请求体超限",str(caught.exception))
        self.assertEqual(len(self.calls),1)

    def test_neighbor_materials_are_complete_and_deduplicated_without_vlm(self):
        import io
        from PIL import Image
        buffer=io.BytesIO()
        Image.new("RGB",(16,16),"green").save(buffer,format="PNG")
        data=buffer.getvalue()
        self.store.put(self.chat,[row(1,"预算请看后面的图"),dict(row(2,"[图片]"),type="图片"),dict(row(3,"[图片]"),type="图片")])
        asset=self.store.media.register(data,data,"image/png")
        with self.store.connect() as conn:
            ids=[r[0] for r in conn.execute("SELECT id FROM messages WHERE kind='图片' ORDER BY seq")]
        for uid in ids:
            self.store.media.link(uid,asset,"image")
        self.store.media.save_analysis(asset["hash"],{"summary":"图中有会议地点及最终金额。"*300},"mock","v1")
        result=self.answer()
        self.assertEqual(result["retrieval_statistics"]["count"],1)
        self.assertEqual(result["analysis_coverage"]["context_messages"],2)
        self.assertEqual(len(result["assets"]),1)
        body=json.loads(self.calls[-1]["messages"][-1]["content"])
        self.assertEqual(body["supplied_assets"],result["assets"])
        self.assertIn("最终金额",body["supplied_assets"][asset["hash"]]["description"])
        self.assertEqual(len(self.calls),2) # Planner and final answer, no VLM.

    def test_over_capacity_preserves_all_matches_and_never_sends_a_partial_analysis(self):
        self.store.put(self.chat,[row(i,"预算"+"完整原文。"*1500) for i in (1,2)])
        with self.assertRaisesRegex(RuntimeError,"已匹配 2 条.*未截断或省略匹配原文"):
            self.answer(max_input_tokens=4096)
        self.assertEqual(len(self.calls),1) # planner only
        self.assertEqual(self.store.stats()[0]["count"],2)
        self.assertEqual(len(self.index.retrieve(self.scope,{"keywords":["预算"]})["records"]),2)

    def test_request_byte_limit_stops_analysis_without_trimming_messages(self):
        from unittest.mock import patch
        class ByteCounter:
            def count(self,text):
                return (len(text)+9)//10
        self.counter=ByteCounter()
        self.store.put(self.chat,[row(i,"预算"+"x"*750000) for i in (1,2,3)])
        with patch("rag_pipeline.MAX_REQUEST_BYTES",2*1024*1024):
            with self.assertRaisesRegex(RuntimeError,"已匹配 3 条.*本地请求体超限"):
                self.answer()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.store.stats()[0]["count"],3)

    def test_api_legacy_rag_budget_is_ignored(self):
        from unittest.mock import patch
        self.store.put(self.chat,[row(i,"预算"+"完整内容。"*40) for i in range(1,101)])
        with patch("rag_pipeline.TokenCounter",return_value=self.counter):
            result=self.client.answer(self.store,"预算",mode="rag",rag_input_tokens=4096)
        self.assertEqual(result["retrieval_statistics"]["count"],100)
        self.assertEqual(len(result["sources"]),100)
        self.assertGreater(result["analysis_coverage"]["estimated_input_tokens"],16000)

    def test_context_rejection_does_not_retry_a_smaller_subset(self):
        from deepseek_api import InputTooLong
        self.store.put(self.chat,[row(i) for i in range(1,101)])
        original=self.client.transport
        def reject(payload):
            if "查询规划器" not in payload["messages"][0]["content"]:
                self.calls.append(payload)
                raise InputTooLong("test context rejection")
            return original(payload)
        self.client.transport=reject
        with self.assertRaises(InputTooLong):
            self.answer()
        self.assertEqual(len(self.calls),2)
        self.assertEqual(len(json.loads(self.calls[-1]["messages"][-1]["content"])["supplied_messages"]),100)

    def test_only_final_answer_reasoning_is_recorded_and_never_sent_as_history(self):
        from conversation import model_history,export_markdown
        self.store.put(self.chat,[row(1)])
        reasoning=[]
        result=self.answer(on_reasoning=reasoning.append)
        self.assertEqual(reasoning,["正式分析的思考"])
        self.assertEqual(result["reasoning_content"],"正式分析的思考")
        result["question"]="预算"
        self.assertIn("正式分析的思考",export_markdown([result]))
        self.answer(history=model_history([result]))
        self.assertNotIn("正式分析的思考",json.dumps(self.calls,ensure_ascii=False))
        self.assertNotIn("辅助调用思考",json.dumps(self.calls,ensure_ascii=False))


if __name__=="__main__":
    unittest.main()
