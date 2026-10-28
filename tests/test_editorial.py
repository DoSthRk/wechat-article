"""编辑、裁图、修订和公开发布边界；全部使用隔离库与假远端。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import app
import db.database as dbmod
from db.database import DatabaseManager, JobStatus
from utils.editorial_document import EditorialError, export_document, import_markdown
from utils.editorial_store import Store, Conflict, digest
from utils import editorial_service as service
from utils import image_packages
from utils.wechat_client import WeChatAPIError, WeChatClient
import editorial_worker


class DocumentTests(unittest.TestCase):
    def test_scientific_superscripts_and_subscripts_are_preserved(self):
        doc=import_markdown('Dose 10<sup>12</sup> vg/kg; H<sub>2</sub>O.')
        result,_=export_document(doc)
        self.assertIn('10<sup>12</sup>',result)
        self.assertIn('H<sub>2</sub>O',result)

    def test_roundtrip_structure_and_caption(self):
        doc = import_markdown('# Main title\n\n## Section\n\nA **bold** word.\n\n[图片:Figure 1 Experiment]\n\n|A|B|\n|---|---|\n|1|2|')
        result, keys = export_document(doc)
        self.assertNotIn('# Main title', result)
        self.assertIn('## Section', result)
        self.assertIn('**bold**', result)
        self.assertEqual(keys, ['main:1'])
        table = doc['content'][-1]
        self.assertEqual(table['content'][0]['content'][0]['content'][0]['type'], 'paragraph')

    def test_script_and_external_image_not_imported(self):
        doc = import_markdown('Text\n\n<script>alert(1)</script>\n\n<img src="https://evil.invalid/x">')
        rendered, _ = export_document(doc)
        self.assertNotIn('script', rendered)
        self.assertNotIn('evil.invalid', rendered)

    def test_invalid_link_and_forged_image_rejected(self):
        for node in [{'type':'image','attrs':{'src':'/etc/passwd'}},
                     {'type':'text','text':'x','marks':[{'type':'link','attrs':{'href':'javascript:alert(1)'}}]},
                     {'type':'text','text':'[图片:Figure 1]'}]:
            with self.assertRaises(EditorialError): export_document({'type':'doc','content':[node]})

    def test_duplicate_figure_and_h1_rejected(self):
        for nodes in [[{'type':'figure','attrs':{'key':'main:1'}}]*2,
                      [{'type':'heading','attrs':{'level':1},'content':[]}]]:
            with self.assertRaises(EditorialError): export_document({'type':'doc','content':nodes})


class EditorialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.db = DatabaseManager(database_url=f'sqlite:///{self.root / "app.db"}')
        self.store = Store(self.root / 'editorial')
        self.output = self.root / 'outputs' / 'paper'; self.output.mkdir(parents=True)
        self.pdf = self.root / 'source.pdf'
        image = Image.new('RGB',(600,800),'white'); ImageDraw.Draw(image).rectangle((40,60,400,450),fill='navy')
        image.save(self.pdf,'PDF',resolution=72)
        (self.output/'article.md').write_text('# Article\n\n## Background\n\nOriginal content.\n\n[图片:Figure 1 Experiment]')
        (self.output/'meta.json').write_text(json.dumps({'source_pdf_sha256':digest(self.pdf.read_bytes())}))
        task = self.db.get_or_create_task('editorial-test')
        record = self.db.upsert_job(task.id,'paper',pdf_path=str(self.pdf),template_id='aav_academic_soft_intro',product_id='purprox_aaveasy_spin_columns',line_id='aav',status=JobStatus.GENERATED)
        self.pk = record.id
        self.db.upsert_article(self.pk,title='Article',digest='Summary',content_dir=str(self.output))
        self.patches=[patch.object(dbmod,'_instance',self.db),patch.object(image_packages,'ARTICLE_CONTENT_DIR',self.output.parent),
            patch('utils.blog_task_lock.LOCK_DIR',self.root/'locks'),patch('utils.editorial_api.Store',return_value=self.store),
            patch('utils.editorial_api.kick'),patch('utils.auto_blog.kick'),patch('utils.lab_sso.session_secret',return_value='test-only-secret'),
            patch('socket.socket.connect',side_effect=AssertionError('network forbidden'))]
        for p in self.patches:p.start()
        self.application=app.create_app(testing=False)
        self.application.config['TRUST_PROXY_IDENTITY']=True
        self.client=self.application.test_client()
        self.headers={'X-GM-LAB-Username':'hqq'}
        self.client.get('/workbench',headers=self.headers)
        with self.client.session_transaction() as session:self.csrf=session['editorial_csrf']
        self.write_headers={**self.headers,'X-CSRF-Token':self.csrf}
        self.initial=service.initialize(self.db,self.store,self.pk,'hqq')

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.db.engine.dispose();self.tmp.cleanup()

    def save(self,title='Edited article',**values):
        head=self.store.head(self.pk);current=self.store.revision(self.pk,head['revision'])
        payload={'expected_revision':head['revision'],'title':title,'digest':'Edited digest','document':current['document'],**values}
        return service.save_draft(self.db,self.store,self.pk,payload,'hqq')

    def cropped(self):
        return service.crop(self.db,self.store,self.pk,{'expected_revision':self.store.head(self.pk)['revision'],'key':'main:1','page':1,'bbox':[35,55,410,460]},'hqq')

    def task(self):
        info=self.cropped()
        return self.store.enqueue('publish',self.pk,info['revision'],{'reviewer':'hqq','confirmed':True,'title':info['title'],'line':'aav','targets':{key:{'status':'pending'} for key in ['zh','wechat','en','ja','ko','ru']}})

    def test_import_preserves_original_without_approving(self):
        self.assertTrue(self.initial['document'])
        self.assertEqual(self.initial['figures'],{})
        self.assertEqual(self.store.history(self.pk)[0]['author'],'hqq')
        self.assertNotEqual(image_packages.get_package(service.source_job(self.db,self.pk))['status'],'approved')

    def test_save_and_crop_do_not_change_generated_article(self):
        before=(self.output/'article.md').read_bytes()
        self.save();result=self.cropped()
        self.assertEqual(result['title'],'Edited article')
        self.assertEqual((self.output/'article.md').read_bytes(),before)
        self.assertEqual(self.db.get_article(self.pk).title,'Article')
        with Image.open(self.store.asset(self.pk,result['figures']['main:1']['asset'])) as image:
            self.assertEqual(image.size,(750,810))

    def test_crop_beyond_source_or_too_small_is_rejected(self):
        for box in [[-1,0,300,400],[0,0,601,800],[0,0,1,1],[0,0,float('nan'),400]]:
            with self.assertRaises(EditorialError):service.crop(self.db,self.store,self.pk,{'expected_revision':self.initial['revision'],'key':'main:1','page':1,'bbox':box},'hqq')

    def test_cover_independent_of_figure(self):
        info=self.cropped();original=info['figures']['main:1']['asset']
        cover=service.crop(self.db,self.store,self.pk,{'expected_revision':info['revision'],'key':'cover','page':1,'bbox':[30,40,450,500]},'hqq')
        self.assertEqual(original,cover['figures']['main:1']['asset'])
        with Image.open(self.store.asset(self.pk,cover['cover_asset'])) as image:self.assertEqual(image.size,(900,383))

    def test_concurrent_save_only_one_writer_wins(self):
        before=self.initial['revision']
        def write(i):
            try:return self.store.save(self.pk,'paper','aav',{**self.initial,'title':f'Edit {i}'},expected=before,author='hqq')['revision']
            except Conflict:return 'conflict'
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(write,range(4)))
        self.assertEqual(results.count('conflict'),3)
        self.assertEqual(len(self.store.history(self.pk)),2)

    def test_restore_creates_new_revision(self):
        edited=self.save()
        restored=self.store.save(self.pk,'paper','aav',self.initial,expected=edited['revision'],author='hqq')
        self.assertNotEqual(restored['revision'],self.initial['revision'])
        self.assertEqual(restored['title'],self.initial['title'])
        self.assertEqual(len(self.store.history(self.pk)),3)

    def test_missing_image_and_source_changes_block_publication(self):
        with self.assertRaises(EditorialError):service.validate_publication(self.db,self.store,self.pk,self.initial['revision'])
        info=self.cropped();self.pdf.write_bytes(b'changed source')
        with self.assertRaises(EditorialError):service.validate_publication(self.db,self.store,self.pk,info['revision'])

    def test_request_idempotent_and_freezes_revision(self):
        task=self.task();self.save('Next edit')
        again=self.store.enqueue('publish',self.pk,task['revision'],task['payload'])
        self.assertEqual(task['id'],again['id'])
        self.assertEqual(self.store.revision(self.pk,task['revision'])['title'],'Article')
        with self.assertRaises(Conflict):self.store.enqueue('publish',self.pk,self.store.head(self.pk)['revision'],task['payload'])

    def test_activated_package_matches_exact_human_revision(self):
        task=self.task();next_edit=self.save('New unpublished title')
        bundle=service.activate_package(self.db,self.store,task)
        approved=image_packages.require_approved_package(service.source_job(self.db,self.pk),bundle,expected_job_pk=self.pk)
        self.assertEqual(approved.manifest['editorial_revision'],task['revision'])
        self.assertEqual(self.db.get_article(self.pk).title,'Article')
        self.assertEqual(self.store.head(self.pk)['revision'],next_edit['revision'])
        self.assertEqual(image_packages.get_package(service.source_job(self.db,self.pk))['approval']['reviewer'],'hqq')

    def test_api_requires_real_identity_line_and_csrf(self):
        url=f'/api/editorial/{self.pk}'
        self.assertEqual(self.client.get(url).status_code,401)
        self.assertEqual(self.client.get(url,headers={'X-GM-LAB-Username':'jhh'}).status_code,403)
        self.assertEqual(self.client.put(url,json={},headers=self.headers).status_code,403)
        self.assertEqual(self.client.get(url,headers=self.headers).status_code,200)
        self.client.set_cookie('gm_article_line','aav')
        self.assertEqual(self.client.get(url).status_code,401)

    def test_status_read_does_not_kick_workers(self):
        with patch('utils.editorial_api.kick') as kick:
            self.client.get('/api/editorial/articles',headers=self.headers)
            self.client.get(f'/api/editorial/{self.pk}',headers=self.headers)
            kick.assert_not_called()

    def test_api_confirmation_records_user_and_no_inline_publication(self):
        info=self.cropped()
        with patch('utils.editorial_api.kick') as kick:
            response=self.client.post(f'/api/editorial/{self.pk}/publish',json={'revision':info['revision'],'confirmed':True},headers=self.write_headers)
        self.assertEqual(response.status_code,202,response.get_json())
        task=response.get_json()['task'];self.assertEqual(task['payload']['reviewer'],'hqq')
        self.assertNotEqual(image_packages.get_package(service.source_job(self.db,self.pk))['status'],'approved')
        kick.assert_called_once_with('publish')

    def test_legacy_mutations_blocked_when_v2_enabled(self):
        self.application.config['EDITORIAL_WORKBENCH']=True
        self.assertEqual(self.client.post('/api/run',json={},headers=self.headers).status_code,409)
        self.assertEqual(self.client.get('/operator',headers=self.headers).location,'/workbench')

    def test_asset_boundary_and_preview_template(self):
        info=self.cropped()
        response=self.client.get(f'/api/editorial/{self.pk}/assets/not-a-hash.jpg',headers=self.headers)
        self.assertEqual(response.status_code,400)
        response=self.client.get(f'/api/editorial/{self.pk}/preview/{info["revision"]}/wechat',headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertIn('data-source-pdf-guide',response.get_data(as_text=True))
        self.assertIn('frame-ancestors',response.headers['Content-Security-Policy'])

    def test_retry_only_failed_target_and_never_uncertain(self):
        task=self.task();payload=task['payload'];payload['targets']['zh']={'status':'done','url':'https://example.test'};payload['targets']['en']={'status':'failed'}
        self.store.update_task(task['id'],status='partial',payload=payload)
        retried=self.store.retry(task['id'],'en');self.assertEqual(retried['payload']['targets']['zh']['status'],'done')
        self.assertEqual(retried['payload']['targets']['en']['status'],'pending')
        self.store.update_task(task['id'],status='uncertain',payload=payload)
        with self.assertRaises(Conflict):self.store.retry(task['id'],'wechat')

    def expected(self,task,phase='draft_created'):
        expected={'content':'<p>Approved body</p><img src="https://mmbiz.qpic.cn/test/0">','title':'Article','thumb_media_id':'thumb'}
        task['payload']['targets']['wechat'].update(phase=phase,media_id='media',expected_asset=self.store.put(self.pk,json.dumps(expected).encode(),'.json'))
        return expected

    def test_wechat_submit_timeout_never_auto_resubmits(self):
        task=self.task();expected=self.expected(task);client=Mock()
        client.get_draft.return_value={'news_item':[expected]};client.submit_publication.side_effect=TimeoutError()
        editorial_worker.publish_wechat(self.db,self.store,task,client,service.source_job(self.db,self.pk),'bundle')
        self.assertEqual(task['payload']['targets']['wechat']['status'],'uncertain')
        editorial_worker.publish_wechat(self.db,self.store,task,client,service.source_job(self.db,self.pk),'bundle')
        self.assertEqual(client.submit_publication.call_count,1)

    def test_wechat_waiting_resumes_same_publish_id_then_verifies(self):
        task=self.task();expected=self.expected(task);client=Mock()
        client.get_draft.return_value={'news_item':[expected]};client.submit_publication.return_value='publish1'
        client.publication_status.return_value={'publish_status':1}
        editorial_worker.publish_wechat(self.db,self.store,task,client,service.source_job(self.db,self.pk),'bundle')
        self.assertEqual(task['payload']['targets']['wechat']['status'],'waiting')
        client.publication_status.return_value={'publish_status':0,'article_id':'article1','article_detail':{'item':[{'article_url':'https://mp.weixin.qq.com/s/test'}]}}
        client.published_article.return_value={'news_item':[expected]}
        editorial_worker.publish_wechat(self.db,self.store,task,client,service.source_job(self.db,self.pk),'bundle')
        self.assertEqual(client.submit_publication.call_count,1)
        self.assertEqual(task['payload']['targets']['wechat']['status'],'done')

    def test_wechat_mismatched_body_does_not_submit(self):
        task=self.task();expected=self.expected(task);client=Mock()
        client.get_draft.return_value={'news_item':[{**expected,'content':'changed'}]}
        with self.assertRaises(EditorialError):editorial_worker.publish_wechat(self.db,self.store,task,client,service.source_job(self.db,self.pk),'bundle')
        client.submit_publication.assert_not_called()

    def test_superseded_release_cannot_retry(self):
        old=self.task();old['payload']['targets']['zh']['status']='failed'
        self.store.update_task(old['id'],status='failed',payload=old['payload'])
        changed=self.save('New revision')
        self.store.enqueue('publish',self.pk,changed['revision'],old['payload'])
        with self.assertRaises(Conflict):self.store.retry(old['id'],'zh')

    def test_missing_human_confirmation_cannot_activate(self):
        task=self.task();task['payload']['confirmed']=False
        with self.assertRaises(EditorialError):service.activate_package(self.db,self.store,task)

    def test_prepare_existing_body_and_revision_are_never_regenerated(self):
        task=self.store.enqueue('prepare',self.pk,'',{'owner':'hqq'})
        edited=self.save('Human edit')
        with patch('batch_processor._generate_one') as generate,patch('utils.image_packages.build_package') as build:
            editorial_worker.run_prepare(self.db,self.store,task)
        generate.assert_not_called();build.assert_not_called()
        self.assertEqual(self.store.head(self.pk)['revision'],edited['revision'])
        self.assertEqual(self.store.task(task['id'])['status'],'done')

    def test_failed_automatic_crop_still_opens_manual_editor(self):
        with self.store.connection(write=True) as conn:conn.execute('DELETE FROM heads WHERE job_pk=?',(self.pk,))
        task=self.store.enqueue('prepare',self.pk,'',{'owner':'hqq'})
        with patch('utils.image_packages.get_package',return_value={}),patch('utils.image_packages.build_package',side_effect=TimeoutError('extract timeout')):
            editorial_worker.run_prepare(self.db,self.store,task)
        self.assertIsNotNone(self.store.head(self.pk))
        self.assertEqual(self.store.task(task['id'])['status'],'done')

    def test_upload_queues_generation_and_duplicate_does_not_overwrite(self):
        image=Image.new('RGB',(600,800),'orange');raw=io.BytesIO();image.save(raw,'PDF',resolution=72)
        def upload():return self.client.post('/api/editorial/upload',headers=self.write_headers,data={'line':'aav','file':(io.BytesIO(raw.getvalue()),'new-paper.pdf')})
        with patch('utils.panel_runner.PDFS_DIR',self.root/'uploads'):
            response=upload();self.assertEqual(response.status_code,202,response.json)
            record=response.json;again=upload()
        self.assertTrue(again.json['duplicate'])
        self.assertEqual(again.json['job_pk'],record['job_pk'])
        self.assertEqual(len(self.store.tasks('prepare')),1)
        self.assertIsNone(self.db.get_article(record['job_pk']))

    def test_permission_preflight_failure_does_not_publish(self):
        task=self.task()
        with patch.object(editorial_worker,'WeChatClient') as client,patch.object(editorial_worker,'activate_package') as activate,patch.object(editorial_worker,'notify'):
            client.return_value.publication_access.side_effect=WeChatAPIError('denied',errcode=48001)
            editorial_worker.run_publish(self.db,self.store,task)
        activate.assert_not_called()
        self.assertEqual(self.store.task(task['id'])['status'],'failed')

    def test_new_pdf_runs_real_generation_persistence_then_stops_for_review(self):
        from core.main import AnalysisResult
        parent=self.db.get_or_create_task('fresh-upload')
        record=self.db.upsert_job(parent.id,'fresh-paper',pdf_path=str(self.pdf),template_id='aav_academic_soft_intro',product_id='purprox_aaveasy_spin_columns',line_id='aav',status=JobStatus.PENDING)
        task=self.store.enqueue('prepare',record.id,'',{'owner':'hqq','line':'aav'})
        analyzer=Mock();analyzer.analyze.return_value=AnalysisResult(job_id='fresh-paper',success=True,markdown='# Generated article\n\n## Results\n\nReviewed later.\n\n[图片:Figure 1 Experiment]',model='test-model')
        with patch('core.main.ArticleAnalyzer',return_value=analyzer),patch('batch_processor.ARTICLE_CONTENT_DIR',str(self.output.parent)),patch('utils.image_packages.build_package',return_value={}):
            editorial_worker.run_prepare(self.db,self.store,task)
        self.assertEqual(analyzer.analyze.call_count,1)
        self.assertEqual(self.db.get_article(record.id).title,'Generated article')
        self.assertIsNotNone(self.store.head(record.id))
        self.assertFalse(self.store.tasks('publish'))
        self.assertEqual(self.store.task(task['id'])['status'],'done')

    def test_channel_failure_does_not_repeat_successes(self):
        task=self.task();workflow=Mock()
        def publish(_job,lang,**kw):
            if lang=='ja':raise RuntimeError('translation backend unavailable')
            kw['receipt_callback']('cms_intent');kw['receipt_callback']('cms_saved')
            return {'url':f'https://example.invalid/blog/{lang}'}
        workflow.publish.side_effect=publish
        def wechat(*args):task['payload']['targets']['wechat'].update(status='done',url='https://mp.weixin.qq.com/s/test')
        with patch('utils.blog_pipeline.BlogWorkflow',return_value=workflow),patch.object(editorial_worker,'WeChatClient'),patch.object(editorial_worker,'publish_wechat',side_effect=wechat),patch.object(editorial_worker,'notify'):
            editorial_worker.run_publish(self.db,self.store,task)
            self.assertEqual(self.store.task(task['id'])['status'],'partial')
            self.assertEqual(task['payload']['targets']['ru']['status'],'done')
            retry=self.store.retry(task['id'],'ja');workflow.reset_mock()
            workflow.publish.side_effect=lambda _job,lang,**kw:{'url':'https://example.invalid/blog/'+lang}
            editorial_worker.run_publish(self.db,self.store,retry)
            self.assertEqual(workflow.publish.call_count,1)
            self.assertEqual(workflow.publish.call_args.args[1],'ja')
            self.assertEqual(self.store.task(task['id'])['status'],'done')

    def test_blog_uncertain_write_is_not_retried_or_replaced(self):
        task=self.task();workflow=Mock()
        def publish(_job,lang,**kw):
            if lang=='zh':
                kw['receipt_callback']('cms_intent')
                raise TimeoutError('response lost')
            return {'url':f'https://example.invalid/blog/{lang}'}
        workflow.publish.side_effect=publish
        with patch('utils.blog_pipeline.BlogWorkflow',return_value=workflow),patch.object(editorial_worker,'WeChatClient'),patch.object(editorial_worker,'notify'):
            editorial_worker.run_publish(self.db,self.store,task)
        self.assertEqual(self.store.task(task['id'])['status'],'uncertain')
        with self.assertRaises(Conflict):self.store.retry(task['id'],'zh')
        next_revision=self.save()
        with self.assertRaises(Conflict):self.store.enqueue('publish',self.pk,next_revision['revision'],task['payload'])


class PublicClientTests(unittest.TestCase):
    def test_publish_methods_never_use_mass_send(self):
        with TemporaryDirectory() as tmp:
            client=WeChatClient(app_id='a',app_secret='b',token_cache_path=f'{tmp}/token')
            with patch.object(client,'get_access_token',return_value='test-token'),patch.object(client,'_http_post_json',return_value=(200,b'{"publish_id":"id"}')) as post:
                self.assertEqual(client.submit_publication('media'),'id')
                self.assertIn('/freepublish/submit?',post.call_args.args[0]);self.assertNotIn('/mass/',post.call_args.args[0])
                self.assertEqual(post.call_args.args[1],{'media_id':'media'})


if __name__=='__main__':unittest.main()
