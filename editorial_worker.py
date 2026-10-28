"""站内编辑工作台后台任务：先生成待审，再执行人已确认的公开发布快照。"""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / '.env')

from db.database import get_db_manager, JobStatus
from utils.editorial_store import Store, now
from utils.editorial_service import initialize, source_job, activate_package, validate_publication
from utils.editorial_document import EditorialError
from utils.blog_task_lock import version_lock
from utils.wechat_client import WeChatClient, WeChatAPIError


def run_prepare(db, store, task):
    pk = task['job_pk']; job = source_job(db, pk)
    if db.get_article(pk) is None:
        from batch_processor import _generate_one
        from core.main import ArticleAnalyzer
        if not _generate_one(db, pk, job, ArticleAnalyzer()):
            raise EditorialError('文章生成未完成，请查看生成诊断后重试')
    if not store.head(pk):
        from utils.image_packages import get_package, build_package
        package = get_package(job)
        if not package.get('manifest'):
            try:
                build_package(job, job_pk=pk)
            except Exception:
                import logging
                logging.getLogger(__name__).exception('Automatic figures unavailable; opening manual editor for job %s', pk)
        initialize(db, store, pk, task['payload']['owner'])
    task['payload']['revision'] = store.head(pk)['revision']
    store.update_task(task['id'], status='done', payload=task['payload'])


def verify_wechat(expected, actual):
    from utils.wechat_draft_repair import _Body, _image_key, _text
    wanted, content = expected['content'], str(actual.get('content') or '')
    if (_text(wanted) != _text(content) or actual.get('title') != expected['title'] or
            actual.get('thumb_media_id') != expected['thumb_media_id'] or
            [_image_key(x) for x in _Body(wanted).images] != [_image_key(x) for x in _Body(content).images]):
        raise EditorialError('公众号回读的正文、图片、标题或封面与审核版本不一致')


def publish_wechat(db, store, task, client, job, bundle_id):
    from batch_processor import _distribute_one, _resolve_wechat_account
    payload = task['payload']; target = payload['targets']['wechat']; pk = task['job_pk']
    def checkpoint(): store.update_task(task['id'], status='running', payload=payload)
    if target.get('phase') in {'draft_intent', 'submit_intent'}:
        target.update(status='uncertain', error='上次请求中断，远端结果待负责人核对，不能盲目重复发文')
        checkpoint(); return
    if not target.get('media_id'):
        def receipt(phase, data):
            if phase == 'intent':
                target.update(phase='draft_intent', expected_asset=store.put(pk, json.dumps(data, ensure_ascii=False).encode(), '.json'))
            elif phase == 'draft': target.update(phase='draft_created', media_id=data['media_id'])
            elif phase == 'error' and data.get('definite'):
                target['phase'] = 'draft_created' if target.get('media_id') else 'draft_failed'
            checkpoint()
        if not _distribute_one(db, pk, job, lambda _account: client,
                argparse.Namespace(placeholder_author=None, placeholder_thumb_media=None),
                expected_package_id=bundle_id, enqueue_followups=False, receipt_callback=receipt):
            if target.get('phase') == 'draft_intent':
                target.update(status='uncertain', error='微信草稿写入结果待核对，未提交公开发布')
                checkpoint(); return
            raise EditorialError('公众号素材准备或回读未完成，尚未公开发布')
    expected = json.loads(store.asset(pk, target['expected_asset']).read_text())
    if not target.get('publish_id'):
        draft = client.get_draft(target['media_id'])
        items = draft.get('news_item') or []
        if not items: raise EditorialError('微信草稿不存在，不能提交公开发布')
        verify_wechat(expected, items[0])
        target['phase'] = 'submit_intent'; checkpoint()
        try:
            target['publish_id'] = client.submit_publication(target['media_id'])
        except WeChatAPIError as exc:
            if exc.errcode is not None:
                target['phase'] = 'submit_rejected'; checkpoint()
                raise EditorialError(f'微信拒绝公开发布（错误码 {exc.errcode}），请核对账号权限或内容') from None
            target.update(status='uncertain', error='公开发布提交结果未确认，请负责人核对；不会自动再次提交')
            checkpoint(); return
        except Exception:
            target.update(status='uncertain', error='公开发布提交结果未确认，请负责人核对；不会自动再次提交')
            checkpoint(); return
        target['phase'] = 'submitted'; checkpoint()
    status = client.publication_status(target['publish_id'])
    code = status.get('publish_status')
    if code == 1:
        target.update(status='waiting', error='微信正在审核发布，可关闭页面等待'); checkpoint(); return
    if code != 0:
        if code in {2, 3, 4}:
            target.update(phase='publish_rejected', rejected_publish_id=target.pop('publish_id'))
            raise EditorialError(f'微信公开发布失败（状态 {code}），请核对内容后处理')
        raise EditorialError(f'微信发布状态异常（状态 {code}），已停止自动重发')
    article_id = status.get('article_id')
    items = (status.get('article_detail') or {}).get('item') or []
    if not article_id or not items: raise EditorialError('微信成功回执缺少文章标识或链接')
    url = str(items[0].get('article_url') or '')
    if urlsplit(url).scheme != 'https' or urlsplit(url).hostname != 'mp.weixin.qq.com':
        raise EditorialError('微信未返回有效的公开文章链接')
    actual = client.published_article(str(article_id))
    news = actual.get('news_item') or []
    if not news: raise EditorialError('公众号已提交，但公开文章回读未完成')
    verify_wechat(expected, news[0])
    target.update(status='done', phase='verified', url=url, article_id=str(article_id), error=None)
    db.upsert_distribution(pk, 'wechat', account=_resolve_wechat_account(job), lang='zh',
                           publish_status='published', external_id=str(article_id), external_url=url,
                           image_package_id=bundle_id, image_verification_status='verified', publish_error=None)
    checkpoint()


def notify(store, task):
    """沿用收件人，使用同一请求/结果状态的独立幂等通知记录。"""
    from utils.blog_notifications import recipient_keys, recipient, send_notice, NotificationError
    import uuid
    payload = task['payload']; targets = payload['targets']
    complete = all(t['status'] == 'done' for t in targets.values())
    phase = 'complete' if complete else 'partial'
    notices = payload.setdefault('notices', {})
    text = '\n'.join(['文章已全部发布' if complete else '文章发布需要处理',
        f'业务线：{payload["line"].upper()}', f'文章：{payload["title"]}',
        *[f'{key}：{item.get("url") or item.get("error") or item["status"]}' for key,item in targets.items()]])
    for key in recipient_keys():
        record = notices.setdefault(f'{phase}:{key}', {'uuid': str(uuid.uuid4()), 'status': 'pending', 'attempts': 0})
        if record['status'] in {'sent', 'uncertain', 'failed'}: continue
        if record.get('first_attempt') and time.time() - record['first_attempt'] > 3300:
            record.update(status='uncertain', error='通知去重窗口已过，请核对收信结果'); continue
        try:
            recipient_id, recipient_type = recipient(key)
            record.setdefault('first_attempt', time.time()); record['attempts'] += 1; record['status'] = 'sending'
            store.update_task(task['id'], status=task['status'], payload=payload)
            message_id = send_notice(SimpleNamespace(recipient_id=recipient_id, recipient_type=recipient_type, delivery_uuid=record['uuid']), text)
            record.update(status='sent', message_id=message_id)
        except NotificationError as exc:
            record.update(status='pending' if exc.retryable and record['attempts'] < 5 else 'failed', error=str(exc))
        store.update_task(task['id'], status=task['status'], payload=payload)


def run_publish(db, store, task):
    from batch_processor import _resolve_wechat_account
    from utils.blog_pipeline import BlogWorkflow
    pk = task['job_pk']; job = source_job(db, pk); payload = task['payload']
    if payload.get('confirmed') is not True or not payload.get('reviewer'):
        raise EditorialError('缺少实名审核确认，不能发布')
    client = WeChatClient(account=_resolve_wechat_account(job))
    targets = payload['targets']
    with version_lock(job.job_id, 'image_job'):
        if not payload.get('bundle_id'):
            try:
                client.publication_access()
                payload['bundle_id'] = activate_package(db, store, task)
                payload.pop('error', None)
                store.update_task(task['id'], status='running', payload=payload)
            except Exception as exc:
                payload['error'] = str(exc) if isinstance(exc, EditorialError) else '发布前检查未通过，请核对公众号公开发布权限与原文配图'
                for item in targets.values():
                    if item['status'] == 'pending': item.update(status='failed', error=payload['error'])
                store.update_task(task['id'], status='failed', payload=payload)
                task['status'] = 'failed'
                notify(store, task)
                return
        bundle = payload['bundle_id']
        from utils.image_packages import require_approved_package
        require_approved_package(job, bundle, expected_job_pk=pk)
        workflow = BlogWorkflow(db)
        for key in ['zh', 'wechat', 'en', 'ja', 'ko', 'ru']:
            target = targets[key]
            if target['status'] not in {'pending', 'running', 'waiting'}: continue
            if target.get('phase') == 'cms_intent':
                target.update(status='uncertain', error='Blog 写入结果待核对，不能重复创建文章')
                continue
            if key == 'wechat' and targets['zh']['status'] != 'done':
                target.update(status='failed', error='中文 Blog 尚未完成，公众号阅读原文链接未就绪'); continue
            target['status'] = 'running'; store.update_task(task['id'], status='running', payload=payload)
            try:
                if key == 'wechat': publish_wechat(db, store, task, client, job, bundle)
                else:
                    if key != 'zh':
                        version = db.get_article_version(pk, key)
                        package = require_approved_package(job, bundle, expected_job_pk=pk)
                        if version.translation_status != 'translated' or version.source_markdown_sha256 != package.source_markdown_sha256:
                            workflow.translate(job.job_id, key, job_pk=pk, expected_package_id=bundle)
                    def receipt(phase):
                        target['phase'] = phase
                        store.update_task(task['id'], status='running', payload=payload)
                    result = workflow.publish(job.job_id, key, job_pk=pk, expected_package_id=bundle, receipt_callback=receipt)
                    target.update(status='done', url=result['url'], error=None)
            except Exception as exc:
                # 有 publish_id 时只回读原发布任务，失败重试不会再次 submit。
                uncertain = target.get('phase') in {'draft_intent','submit_intent','cms_intent'}
                target.update(status='uncertain' if uncertain else 'failed', error='远端写入结果待负责人核对，不会自动重复发布' if uncertain else str(exc) if isinstance(exc, (EditorialError, WeChatAPIError)) else '此渠道处理未完成，请重试或联系负责人查看诊断')
                import logging; logging.getLogger(__name__).exception('editorial target %s failed for request %s', key, task['id'])
            store.update_task(task['id'], status='running', payload=payload)
    statuses = [item['status'] for item in targets.values()]
    state = 'done' if all(s == 'done' for s in statuses) else 'queued' if 'waiting' in statuses else 'uncertain' if 'uncertain' in statuses else 'partial' if 'done' in statuses else 'failed'
    store.update_task(task['id'], status=state, payload=payload); task['status'] = state
    if state != 'queued': notify(store, task)


def main(kind):
    store = Store()
    # 每条队列只有一个持锁执行器；进程崩溃后内核释放，重启从阶段回执恢复。
    with (store.root / f'{kind}.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        db = get_db_manager()
        if kind == 'publish':
            capabilities = {}
            for line, account in [('aav','aav'),('solidex','immune')]:
                try:
                    WeChatClient(account=account).publication_access()
                    capabilities[line] = {'read_access': True, 'checked_at': now(), 'message': '发布列表权限检查通过；正式提交仍以微信回执为准'}
                except Exception as exc:
                    capabilities[line] = {'read_access': False, 'checked_at': now(),
                        'code': exc.errcode if isinstance(exc,WeChatAPIError) else None,
                        'message': '公众号公开发布权限检查未通过，请负责人核对账号权限或连接'}
            temporary = store.root / 'capabilities.tmp'
            temporary.write_text(json.dumps(capabilities,ensure_ascii=False));temporary.replace(store.root/'capabilities.json')
        while True:
            tasks = [t for t in reversed(store.tasks(kind)) if t['status'] in {'queued', 'running'}]
            if not tasks:
                if kind == 'publish':
                    for task in store.tasks('publish'):
                        if task['status'] not in {'queued','running'} and any(n['status'] in {'pending','sending'} for n in task['payload'].get('notices', {}).values()):
                            notify(store, task)
                time.sleep(5)
                continue
            for task in tasks:
                store.update_task(task['id'], status='running', payload=task['payload'])
                try:
                    (run_prepare if kind == 'prepare' else run_publish)(db, store, task)
                except Exception as exc:
                    import logging; logging.getLogger(__name__).exception('editorial request %s failed', task['id'])
                    payload = task['payload']; payload['error'] = str(exc) if isinstance(exc, EditorialError) else '后台处理未完成，请联系负责人查看任务诊断'
                    for item in payload.get('targets', {}).values():
                        if item['status'] in {'pending', 'running', 'waiting'}:
                            item.update(status='uncertain' if item.get('phase') in {'draft_intent','submit_intent','cms_intent'} else 'failed', error=payload['error'])
                    state = 'uncertain' if any(i['status']=='uncertain' for i in payload.get('targets',{}).values()) else 'failed'
                    store.update_task(task['id'], status=state, payload=payload)
            if kind == 'publish': time.sleep(10)


if __name__ == '__main__':
    if len(sys.argv) != 2 or sys.argv[1] not in {'prepare', 'publish'}: raise SystemExit(2)
    main(sys.argv[1])
