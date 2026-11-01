"""Prove final speech starts before turn completion and never repeats or leaks commentary."""
import unittest
from dot_client import DotBrain, DotError, EventQueue
from minimax_client import Cancelled
from test_dot_client import FakeRpc


class StreamRpc(FakeRpc):
    def event(self,method,**fields):self.events.put({'method':method,'params':{'threadId':'voice-thread','turnId':'stream-turn',**fields}})
    def request(self,method,params,timeout=15):
        if method!='turn/start':return super().request(method,params,timeout)
        self.event('item/started',item={'id':'comment','type':'agentMessage','phase':'commentary'})
        self.event('item/agentMessage/delta',itemId='comment',delta='不应播报的思考过程。')
        self.event('item/started',item={'id':'final','type':'agentMessage','phase':'final_answer'})
        self.event('item/agentMessage/delta',itemId='final',delta='这是第一句。')
        return {'turn':{'id':'stream-turn'}}
    def complete(self,text='这是第一句。这是第二句。',status='completed'):
        self.event('item/completed',item={'id':'final','type':'agentMessage','phase':'final_answer','text':text})
        self.event('turn/completed',turn={'id':'stream-turn','status':status})


class StreamTests(unittest.TestCase):
    def test_first_sentence_precedes_completion_and_full_result_is_not_spoken_twice(self):
        rpc=StreamRpc();brain=DotBrain(rpc);updates=[];stream=brain.clauses('测试',[],on_web=lambda k,v:updates.append((k,v)))
        self.assertEqual(next(stream),'这是第一句。');self.assertFalse(brain.last_metrics.get('completed'))
        rpc.event('item/agentMessage/delta',itemId='final',delta='这是第二句。');rpc.complete()
        self.assertEqual(''.join(stream),'这是第二句。');self.assertTrue(brain.last_metrics['completed'])
        self.assertEqual(next(v['text'] for k,v in updates if k=='dot_result'),'这是第一句。这是第二句。')
    def test_changed_authoritative_final_is_reported_as_failure(self):
        rpc=StreamRpc();updates=[];stream=DotBrain(rpc).clauses('测试',[],on_web=lambda k,v:updates.append(k));self.assertEqual(next(stream),'这是第一句。');rpc.complete('改过的内容。')
        with self.assertRaises(DotError):list(stream)
        self.assertNotIn('dot_result',updates)
    def test_explicit_nonfinal_messages_cannot_become_completed_answers(self):
        class CompletedRpc(FakeRpc):
            def request(self,method,params,timeout=15):
                if method!='turn/start':return super().request(method,params,timeout)
                for item in self.items:
                    self.events.put({'method':'item/completed','params':{'threadId':'voice-thread','turnId':'t','item':item}})
                self.events.put({'method':'turn/completed','params':{'threadId':'voice-thread','turn':{'id':'t','status':'completed'}}})
                return {'turn':{'id':'t'}}
        for extra in ([],[{'type':'agentMessage','text':'阶段不明确的内容'}]):
            with self.subTest(extra=extra):
                rpc=CompletedRpc();rpc.items=[{'type':'agentMessage','phase':'commentary','text':'尚在处理的过程说明'}]+extra;updates=[]
                with self.assertRaises(DotError):list(DotBrain(rpc).clauses('测试',[],on_web=lambda k,v:updates.append(k)))
                self.assertNotIn('dot_result',updates)
        rpc=CompletedRpc();rpc.items=[{'type':'agentMessage','text':'旧协议的最终回答。'}]
        self.assertEqual(''.join(DotBrain(rpc).clauses('测试',[])),'旧协议的最终回答。')
    def test_cancel_after_first_sentence_stops_remaining_speech(self):
        rpc=StreamRpc();brain=DotBrain(rpc);stream=brain.clauses('测试',[]);next(stream);brain.cancel();rpc.complete()
        with self.assertRaises(Cancelled):list(stream)
    def test_final_phase_from_started_item_survives_missing_completion_phase(self):
        rpc=StreamRpc();brain=DotBrain(rpc);stream=brain.clauses('测试',[])
        self.assertEqual(next(stream),'这是第一句。')
        rpc.event('item/completed',item={'id':'final','type':'agentMessage','text':'这是第一句。这是第二句。'})
        rpc.event('turn/completed',turn={'id':'stream-turn','status':'completed'})
        self.assertEqual(''.join(stream),'这是第二句。');self.assertTrue(brain.last_metrics['completed'])
    def test_failed_turn_after_stream_cannot_publish_success_receipt(self):
        rpc=StreamRpc();brain=DotBrain(rpc);updates=[];stream=brain.clauses('测试',[],on_web=lambda k,v:updates.append(k));next(stream);rpc.complete(status='failed')
        with self.assertRaises(DotError):list(stream)
        self.assertNotIn('dot_result',updates)
    def test_backpressure_coalesces_thousands_of_deltas_in_order(self):
        events=EventQueue(maxsize=2)
        for _ in range(5000):events.put_nowait({'method':'item/agentMessage/delta','params':{'threadId':'t','turnId':'r','itemId':'i','delta':'字'}})
        events.put_nowait({'method':'turn/completed','params':{}})
        self.assertEqual(events.qsize(),2);self.assertEqual(events.get()['params']['delta'],'字'*5000);self.assertEqual(events.get()['method'],'turn/completed')
    def test_distinct_item_ids_are_never_coalesced(self):
        events=EventQueue()
        for ident in ['a','b']:events.put_nowait({'method':'item/agentMessage/delta','params':{'itemId':ident,'delta':ident}})
        self.assertEqual(events.qsize(),2)


if __name__=='__main__':unittest.main()
