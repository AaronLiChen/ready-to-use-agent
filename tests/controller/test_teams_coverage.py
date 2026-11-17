import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.parse

import workflow as w
import teams_intake as t
import teams_coverage as c


class TeamsCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.addCleanup(patch.stopall)
        self.root=Path(self.temp.name);patch.object(w,'ROOT',self.root).start();patch.object(w,'notify').start()
        patch.object(t,'fetch_many',side_effect=lambda config,paths:[t.fetch(config,path) for path in paths]).start()
        self.config={'account':'me@example.invalid','self_id':'me','since':'2026-11-10T08:00:00Z','github_owners':['Azure']}
        self.channel={'kind':'channel','team_id':'team','channel_id':'workload','name':'Workload'}
        self.chat={'kind':'chat','chat_id':'dm','chat_type':'oneOnOne','name':'Direct'}
        self.url='https://github.com/Azure/sample-repo/pull/314'

    def message(self,ident='1',text=None,**extra):
        value={'id':ident,'createdDateTime':'2026-11-11T18:47:43Z','lastModifiedDateTime':'2026-11-11T18:47:43Z',
               'from':{'user':{'id':'other'}},'body':{'content':text or 'Can I please get a review '+self.url},
               'mentions':[{'mentioned':{'user':{'id':'me'}}}]}
        value.update(extra);return value

    def test_graph_odata_reply_cursor_is_normalized_without_widening_scope(self):
        value="/teams('team')/channels('19:channel@thread.skype')/messages('123')/replies?$skipToken=next"
        expected='/teams/team/channels/19%3Achannel%40thread.skype/messages/123/replies?$skipToken=next'
        self.assertEqual(t.graph_path(value),expected)
        self.assertEqual(t.graph_path('https://graph.microsoft.com/beta'+value),expected)
        with self.assertRaises(t.ReadError):t.graph_path('https://evil.example/beta'+value)
        with self.assertRaises(t.ReadError):t.graph_path('https://graph.microsoft.com/v1.0/users/another-user/chats')

    def test_prefetched_read_uses_the_batch_start_as_its_watermark(self):
        started='2026-11-11T09:00:00+00:00'
        with patch.object(t,'fetch',return_value={'value':[]}):result=c.scan_source(self.config,self.chat,{},time.monotonic()+10,started_at=started)
        self.assertEqual(result['last_success_at'],started)

    def test_root_context_is_reused_without_an_extra_parent_api_request(self):
        root=self.message(text=self.url,mentions=[],replies=[self.message('2',text='Could you please review this?')])
        with patch.object(t,'fetch',return_value={'value':[root]}) as fetch:
            result=c.scan_source(self.config,self.channel,{},time.monotonic()+10)
        self.assertTrue(result['complete']);self.assertEqual(fetch.call_count,1)
        self.assertEqual(t.inbox()[0]['url'],self.url)

    def test_transient_team_discovery_failure_keeps_known_channels_and_a_gap(self):
        config={**self.config,'background_silent_only':True,'direct_channel_ids':['workload']}
        state={'channels':{'workload':self.channel}}
        def collection(config,path,limit=30):
            if path.startswith('/me/joinedTeams'):return [{'id':'team','displayName':'Known Team'}]
            raise t.ReadError('Shared provider cooldown')
        with patch.object(t,'collection',side_effect=collection):t.discover(config,state)
        self.assertIn('workload',state['channels']);self.assertIn('team',state['discovery_gaps'])
        self.assertEqual(state['priority_channels'][0]['channel_id'],'workload')

    def test_explicit_workload_channel_does_not_depend_on_pr_name(self):
        self.assertFalse(t.priority_channel(self.config,self.channel))
        self.config['direct_channel_ids']=['workload']
        self.assertTrue(t.priority_channel(self.config,self.channel))

    def test_non_pr_named_channel_mention_is_read_and_recorded(self):
        with patch.object(t,'fetch',return_value={'value':[self.message()]}):
            result=c.scan_source(self.config,self.channel,{},time.monotonic()+10)
        self.assertTrue(result['complete']);self.assertEqual(t.inbox()[0]['url'],self.url)

    def test_direct_chat_does_not_require_a_mention(self):
        with patch.object(t,'fetch',return_value={'value':[self.message(mentions=[])]}):
            result=c.scan_source(self.config,self.chat,{},time.monotonic()+10)
        self.assertTrue(result['complete']);self.assertEqual(len(t.inbox()),1)

    def test_group_chat_without_direct_request_is_not_authority(self):
        source={**self.chat,'chat_type':'group'}
        with patch.object(t,'fetch',return_value={'value':[self.message(mentions=[])]}):c.scan_source(self.config,source,{},time.monotonic()+10)
        self.assertEqual(t.inbox(),[])

    def test_reply_pagination_is_read_not_only_warned_about(self):
        root=c.base_path(self.channel)+'/1'
        pages=[{'value':[self.message(text='Root context',mentions=[],replies=[],**{'replies@odata.nextLink':root+'/replies?$skiptoken=next'})]},
               {'value':[self.message('2')]}]
        with patch.object(t,'fetch',side_effect=pages):result=c.scan_source(self.config,self.channel,{},time.monotonic()+10)
        self.assertTrue(result['complete'])
        evidence=next(iter(t.inbox()[0]['evidence'].values()))
        self.assertEqual(evidence['source']['graph_path'],root+'/replies/2')

    def test_old_root_with_fresh_reply_is_not_discarded(self):
        first=self.message(createdDateTime='2025-01-01T00:00:00Z',lastModifiedDateTime='2025-01-01T00:00:00Z',text='Old root',mentions=[],replies=[self.message('2')])
        with patch.object(t,'fetch',return_value={'value':[first]}):result=c.scan_source(self.config,self.channel,{},time.monotonic()+10)
        self.assertTrue(result['complete']);self.assertEqual(len(t.inbox()),1)

    def test_message_cursor_survives_page_bound_without_advancing_watermark(self):
        def fetch(config,path):
            page=int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get('page',['1'])[0])
            return {'value':[],'@odata.nextLink':c.base_path(self.chat)+'?page='+str(page+1)} if page<4 else {'value':[]}
        with patch.object(t,'fetch',side_effect=fetch):
            first=c.scan_source(self.config,self.chat,{},time.monotonic()+10)
            self.assertFalse(first['complete']);self.assertNotIn('last_success_at',first)
            self.assertIn('page=4',first['next'])
            second=c.scan_source(self.config,self.chat,first,time.monotonic()+10)
        self.assertTrue(second['complete']);self.assertEqual(second['last_success_at'],first['cycle_started_at'])

    def test_failed_page_preserves_the_original_watermark_and_cursor(self):
        previous={'last_success_at':'2026-11-10T09:00:00Z'}
        with patch.object(t,'fetch',side_effect=t.ReadError('WorkIQ unavailable')):
            result=c.scan_source(self.config,self.chat,previous,time.monotonic()+10)
        self.assertFalse(result['complete']);self.assertEqual(result['last_success_at'],previous['last_success_at'])
        self.assertTrue(result['next']);self.assertEqual(previous,{'last_success_at':'2026-11-10T09:00:00Z'})

    def test_all_known_chats_and_channels_are_scheduled_without_name_filter(self):
        state={'channels':{'workload':self.channel},'chat_catalogue':{'items':{'dm':self.chat},
                'last_complete_epoch':time.time(),'last_complete_at':w.utc()}}
        with patch.object(t,'fetch',return_value={'value':[]}):c.poll_sources(self.config,state,time.monotonic()+10)
        self.assertEqual(set(state['source_coverage']),{'workload','dm'})
        self.assertEqual(state['all_source_coverage']['sources_attempted_this_poll'],2)
        self.assertTrue(state['all_source_coverage']['complete'])

    def test_failed_or_unfinished_sources_are_not_called_complete(self):
        state={'channels':{'workload':self.channel},'chat_catalogue':{'items':{},'last_complete_epoch':time.time(),'last_complete_at':w.utc()}}
        with patch.object(t,'fetch',side_effect=t.ReadError('HTTP 403')):c.poll_sources(self.config,state,time.monotonic()+10)
        self.assertFalse(state['all_source_coverage']['complete']);self.assertEqual(state['all_source_coverage']['failed_sources'],1)
        self.assertEqual(state['coverage_gaps']['workload'],'HTTP 403')

    def test_chat_catalogue_pagination_resumes_instead_of_restarting_front_page(self):
        def fetch(config,path):
            page=int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get('page',['1'])[0])
            data={'value':[{'id':'chat-'+str(page),'chatType':'oneOnOne'}]}
            if page<11:data['@odata.nextLink']='/me/chats?page='+str(page+1)
            return data
        state={}
        with patch.object(t,'fetch',side_effect=fetch):
            c.discover_chats(self.config,state,time.monotonic()+10)
            self.assertIn('page=11',state['chat_catalogue']['next']);self.assertNotIn('last_complete_at',state['chat_catalogue'])
            c.discover_chats(self.config,state,time.monotonic()+10)
        self.assertEqual(len(state['chat_catalogue']['items']),11);self.assertIsNone(state['chat_catalogue']['next'])

    def test_non_review_messages_are_scanned_but_do_not_create_jobs(self):
        with patch.object(t,'fetch',return_value={'value':[self.message(text='Hello Example Reviewer',mentions=[])]}):c.scan_source(self.config,self.chat,{},time.monotonic()+10)
        self.assertEqual(t.inbox(),[]);self.assertEqual(w.jobs(),[])


from controller.fixtures import configured
TeamsCoverageTests.setUp = configured(TeamsCoverageTests.setUp)

if __name__=='__main__':unittest.main()
