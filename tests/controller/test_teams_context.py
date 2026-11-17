from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import workflow as w
import teams_intake as t


class TeamsContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.addCleanup(patch.stopall)
        self.root=Path(self.temp.name);patch.object(w,'ROOT',self.root).start();patch.object(w,'notify').start()
        self.config={'account':'me@example.invalid','self_id':'me','since':'2026-11-10T08:00:00Z','github_owners':['Azure']}
        self.base='/chats/chat-one/messages'
        self.source={'kind':'chat','chat_id':'chat-one','chat_type':'oneOnOne','message_base':self.base,'graph_path':self.base+'/1'}
        self.url='https://dev.azure.com/example-org/ExampleProject/_git/repo/pullrequest/42'
        self.anchor=self.message('1',0,'Can you dispatch agents to review some PRs?')
        self.link=self.message('2',4,self.url)

    def message(self,ident,seconds,text,sender='peer',**extra):
        stamp=(datetime(2026,11,10,9,tzinfo=timezone.utc)+timedelta(seconds=seconds)).isoformat()
        return {'id':ident,'createdDateTime':stamp,'from':{'user':{'id':sender}},'body':{'content':text},**extra}

    def match(self,messages):return t.conversation_followups(self.config,self.anchor,self.source,messages,self.base)

    def test_author_pattern_request_then_link_four_seconds_later(self):
        context=self.match([self.link,self.anchor])
        self.assertEqual([c['url'] for c in context],[self.url])
        self.assertEqual(context[0]['graph_path'],self.base+'/2')

    def test_order_does_not_depend_on_api_newest_first_order(self):
        self.assertEqual(self.match([self.anchor,self.link]),self.match([self.link,self.anchor]))

    def test_multiple_followup_messages_can_supply_a_pr_list(self):
        second=self.message('3',8,self.url.replace('/42','/43'))
        self.assertEqual(len(self.match([second,self.link,self.anchor])),2)

    def test_other_sender_does_not_supply_the_link(self):
        self.link['from']['user']['id']='someone-else'
        self.assertEqual(self.match([self.anchor,self.link]),[])

    def test_other_chat_does_not_supply_the_link(self):
        self.link['chatId']='chat-two'
        self.assertEqual(self.match([self.anchor,self.link]),[])

    def test_late_or_preceding_link_is_not_assumed_to_be_the_request(self):
        for delay in [-1,301]:
            self.assertEqual(self.match([self.anchor,self.message('2',delay,self.url)]),[])

    def test_new_request_ends_the_previous_missing_link_context(self):
        newer=self.message('3',2,'Could you review another change?')
        self.assertEqual(self.match([self.anchor,newer,self.link]),[])

    def test_cancellation_before_or_after_link_cancels_context(self):
        for seconds in [2,8]:
            cancel=self.message('3',seconds,'No need to review this')
            self.assertEqual(self.match([self.anchor,self.link,cancel]),[])
        self.assertEqual(self.match([self.anchor,self.link,self.message('3',8,'Do not review this',sender='me')]),[])

    def test_quoted_and_fyi_only_links_do_not_become_assignments(self):
        self.assertEqual(self.match([self.anchor,self.message('2',4,'<blockquote>'+self.url+'</blockquote>')]),[])
        self.assertEqual(self.match([self.anchor,self.message('2',4,'FYI only '+self.url)]),[])

    def test_different_mentioned_recipient_is_not_assumed_to_be_me(self):
        self.link['mentions']=[{'mentioned':{'user':{'id':'other-recipient'}}}]
        self.assertEqual(self.match([self.anchor,self.link]),[])

    def test_different_reply_thread_is_not_assumed_to_match(self):
        self.link['replyToId']='unrelated-root'
        self.assertEqual(self.match([self.anchor,self.link]),[])

    def test_group_chat_still_requires_a_request_directed_to_me(self):
        self.source['chat_type']='group'
        self.assertEqual(self.match([self.anchor,self.link]),[])
        self.anchor['mentions']=[{'mentioned':{'user':{'id':'me'}}}]
        self.assertEqual(len(self.match([self.anchor,self.link])),1)

    def test_reaction_modification_does_not_move_the_link_time(self):
        self.link['lastModifiedDateTime']='2026-11-11T09:00:00Z'
        self.assertEqual(len(self.match([self.anchor,self.link])),1)

    def test_intervening_message_bound_is_not_silently_exceeded(self):
        messages=[self.anchor]+[self.message(str(i+3),i+1,'unrelated',sender='someone-else') for i in range(20)]
        messages.append(self.message('99',30,self.url))
        self.assertEqual(self.match(messages),[])

    def fake_fetch(self,config,path):
        if path.startswith('/me?'):return {'id':'me','userPrincipalName':config['account']}
        if path==self.source['graph_path']:return self.anchor
        if path.startswith(self.base+'?'):return {'value':[self.link,self.anchor]}
        raise AssertionError('Unexpected scoped path: '+path)

    def test_held_request_backfills_without_changing_cutoff_or_scan_watermarks(self):
        t.record(self.config,self.anchor,self.source)
        original=t.inbox()[0]['id'];state={'last_chat_scan':'do-not-reset','message_cursors':{}}
        w.save(t.state_path(),state)
        with patch.object(t,'fetch',side_effect=self.fake_fetch):t.resolve_missing_context(self.config,time.monotonic()+10)
        rows=t.inbox();held=next(r for r in rows if r['id']==original)
        self.assertEqual(held['status'],'context_linked')
        paired=next(r for r in rows if r.get('url')==self.url)
        self.assertEqual(paired['status'],'pending')
        self.assertEqual(paired['evidence'][next(iter(paired['evidence']))]['context_sources'][0]['kind'],'conversation_followup')
        self.assertEqual(w.load(t.state_path()),state)
        self.assertEqual(self.config['since'],'2026-11-10T08:00:00Z')

    def test_enrollment_reverification_refetches_the_live_context(self):
        t.record(self.config,self.anchor,self.source)
        with patch.object(t,'fetch',side_effect=self.fake_fetch):
            t.resolve_missing_context(self.config,time.monotonic()+10)
            paired=next(r for r in t.inbox() if r.get('url')==self.url)
            self.assertTrue(t.verify_request(self.config,paired))
            self.link['body']['content']='The link was removed'
            self.assertFalse(t.verify_request(self.config,paired))

    def test_existing_job_is_linked_instead_of_prepared_again(self):
        w.save(w.job_path('existing'),{'id':'existing','url':self.url,'status':'waiting'})
        t.record(self.config,self.anchor,self.source)
        with patch.object(t,'fetch',side_effect=self.fake_fetch):t.resolve_missing_context(self.config,time.monotonic()+10)
        paired=next(r for r in t.inbox() if r.get('url')==self.url)
        self.assertEqual(paired['status'],'linked');self.assertEqual(paired['job'],'existing')


from controller.fixtures import configured
TeamsContextTests.setUp = configured(TeamsContextTests.setUp)

if __name__=='__main__':unittest.main()
