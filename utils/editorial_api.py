"""工作台 V2 HTTP 边界：实名、业务线、CSRF、修订 CAS 与只读状态。"""
from pathlib import Path
import secrets
import json
import re

from flask import abort, jsonify, redirect, render_template, request, send_file, session, url_for

from db.database import get_db_manager, JobStatus
from utils.editorial_document import EditorialError
from utils.editorial_store import Store, Conflict, digest, encode, now
from utils import editorial_service as service
from utils.editorial_runner import kick


def register(app, reviewer, access, has_identity):
    def identity():
        return reviewer()

    def allowed(line):
        lines = access()
        if line not in {'aav', 'solidex'} or (lines is not None and line not in lines): abort(403)

    def selected_line(required=True):
        # 每个页面把自身业务线随请求传入，不用会被另一标签页覆盖的 session/cookie。
        header, query = request.headers.get('X-Editorial-Line'), request.args.get('line')
        if header and query and header != query: raise EditorialError('业务线参数不一致')
        line = header or query
        if not line:
            lines = access()
            if lines and len(lines) == 1: line = next(iter(lines))
        if line: allowed(line)
        elif required: raise EditorialError('请先进入 AAV 或 SOLIDEX 的独立工作台')
        return line

    def job(pk):
        identity()
        db = get_db_manager()
        record = db.get_job(pk)
        if not record: abort(404)
        source = service.source_job(db, pk)
        allowed(source.line)
        selected = selected_line(required=request.method not in {'GET','HEAD','OPTIONS'})
        if selected and source.line != selected: abort(403)
        if db.find_job_pk(source.job_id) != pk: raise Conflict('这是历史任务记录，请返回工作台打开当前文章')
        return db, source

    def csrf():
        identity()
        expected = session.get('editorial_csrf', '')
        if not expected or not secrets.compare_digest(expected, request.headers.get('X-CSRF-Token', '')): abort(403)

    def data():
        csrf()
        if request.content_length and request.content_length > 800000: raise EditorialError('编辑内容超出大小限制')
        value = request.get_json(silent=True)
        if not isinstance(value, dict): raise EditorialError('请求必须是 JSON 对象')
        return value

    def is_inbox(item, latest, head):
        generating=latest and latest['kind']=='prepare' and latest['status'] in {'queued','running','failed'}
        published=any(d.get('external_url') and d.get('publish_status')=='published' for d in item.get('distributions',[]))
        record=(latest and latest['kind']=='publish' and latest['status']!='cancelled') or (not head and published)
        return not generating and not record

    @app.errorhandler(EditorialError)
    def error(exc):
        return jsonify(ok=False, error=str(exc), conflict=isinstance(exc, Conflict)), 409 if isinstance(exc, Conflict) else 400

    @app.get('/workbench')
    @app.get('/workbench/<line_id>')
    def editorial_page(line_id=None):
        if line_id and line_id not in {'aav','solidex'}: abort(404)
        if not has_identity() and not app.config['TESTING']:
            return redirect(url_for('article_auth_start', next=url_for('editorial_page',line_id=line_id)))
        who = identity()
        lines = access()
        if line_id is None:
            if lines and len(lines)==1:
                return redirect(url_for('editorial_page',line_id=next(iter(lines))))
            return render_template('editorial_lines.html',username=who)
        allowed(line_id)
        session.setdefault('editorial_csrf', secrets.token_urlsafe(32))
        return render_template('editorial.html', username=who, line_id=line_id,
                               can_switch=lines is None or len(lines)>1, csrf=session['editorial_csrf'])

    @app.get('/api/editorial/articles')
    def editorial_articles():
        identity(); line_filter = selected_line(); db = get_db_manager(); store = Store()
        records, _ = db.list_article_overview(page_size=10000)
        result = []; seen = set(); tasks = store.tasks()
        archived = store.archived_jobs(line_filter)
        for item in records:
            if item['job_id'] in seen: continue
            seen.add(item['job_id'])
            pk = db.find_job_pk(item['job_id'])
            try: line = service.source_job(db, pk).line
            except Exception: continue
            if line != line_filter: continue
            head = store.head(pk)
            related = [t for t in tasks if t['job_pk'] == pk]
            latest = related[0] if related else None
            title = item['title']
            if head: title = store.revision(pk, head['revision'])['title']
            published = any(d['external_url'] and d['publish_status']=='published' for d in item['distributions'])
            result.append({'job_pk':pk,'job_id':item['job_id'],'line':line,'title':title or item['job_id'],
                'has_article':bool(item['title']), 'revision':head['revision'] if head else None,
                'archived':pk in archived, 'can_archive':is_inbox(item,latest,head) and pk not in archived,
                'legacy_published':published, 'distributions':item['distributions'], 'task':latest,
                'held':item['job_id'] in {'免疫客文章-4-3','免疫客文章-4-4'}})
        return jsonify(ok=True, articles=result)

    @app.post('/api/editorial/<int:pk>/inbox-archive')
    @app.post('/api/editorial/<int:pk>/inbox-restore')
    def editorial_inbox_archive(pk):
        body=data(); db,source=job(pk); store=Store()
        archived=request.path.endswith('/inbox-archive')
        records,_=db.list_article_overview(page_size=10000)
        item=next((r for r in records if r['job_id']==source.job_id),None)
        if item is None: abort(404)
        related=[t for t in store.tasks() if t['job_pk']==pk]
        if archived and not is_inbox(item,related[0] if related else None,store.head(pk)):
            raise Conflict('仅可移除审核草稿箱中的文章，不能删除生成任务或发布记录')
        from utils.auto_blog import AutoBlogTask
        with db.get_session() as conn:
            if conn.query(AutoBlogTask).filter(AutoBlogTask.job_pk==pk,AutoBlogTask.status.in_(('queued','running'))).first():
                raise Conflict('旧版发布任务仍在处理此文章，暂不能移出草稿箱')
        store.set_inbox_archived(pk,source.line,archived,expected=body.get('expected_revision'),
                                author=identity(),snapshot={'job_pk':pk,'job_id':source.job_id,'title':item['title'],
                                'revision':body.get('expected_revision'),'line':source.line,'distributions':item['distributions']})
        return jsonify(ok=True,archived=archived,job_pk=pk)

    @app.get('/api/editorial/capabilities')
    def editorial_capabilities():
        # 只读诊断允许已有业务线查看身份；不赋予编辑或发布权限。
        lines = access()
        path = Store().root / 'capabilities.json'
        values = json.loads(path.read_text()) if path.exists() else {}
        return jsonify(ok=True, accounts={k:v for k,v in values.items() if lines is None or k in lines})

    @app.post('/api/editorial/upload')
    def editorial_upload():
        csrf(); line = request.form.get('line', ''); allowed(line)
        if selected_line() != line: abort(403)
        file = request.files.get('file')
        if not file or not file.filename.lower().endswith('.pdf'): raise EditorialError('请选择 PDF 文件')
        raw = file.read()
        if not raw or b'%PDF-' not in raw[:1024]: raise EditorialError('文件不是有效 PDF')
        sha = digest(raw); store = Store(); db = get_db_manager()
        from utils.blog_task_lock import version_lock
        from utils.line_loader import load_line_by_id
        from utils.panel_runner import LINES_DIR, PDFS_DIR
        with version_lock(line, 'editorial_upload'):
            with store.connection() as conn:
                existing = conn.execute('SELECT job_pk FROM uploads WHERE line=? AND source_hash=?', (line,sha)).fetchone()
            if existing: return jsonify(ok=True, job_pk=existing['job_pk'], duplicate=True)
            # 迁移期也识别旧工作台上传的相同 PDF，绝不覆盖现有正文。
            records, _ = db.list_article_overview(page_size=10000)
            for record in records:
                pk = db.find_job_pk(record['job_id'])
                try:
                    old = service.source_job(db, pk)
                    if old.line == line and digest(Path(old.pdf).read_bytes()) == sha:
                        return jsonify(ok=True, job_pk=pk, duplicate=True)
                except (OSError, EditorialError): continue
            config = load_line_by_id(str(LINES_DIR), line)
            stem = re.sub(r'[^\w\-\u4e00-\u9fff]', '-', Path(file.filename).stem)[:70].strip('-') or 'article'
            job_id = f'{line}-{stem}-{sha[:12]}'
            folder = PDFS_DIR / config.extra['pdf_folder']; folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f'{job_id}.pdf'
            if dest.exists() and digest(dest.read_bytes()) != sha: raise Conflict('文件名冲突，未覆盖已有 PDF')
            if not dest.exists(): dest.write_bytes(raw)
            task = db.get_or_create_task(f'editorial-{line}')
            record = db.upsert_job(task.id, job_id, pdf_path=str(dest), template_id=config.template,
                                   product_id=config.product, line_id=line, status=JobStatus.PENDING)
            with store.connection(write=True) as conn:
                conn.execute('INSERT INTO uploads VALUES(?,?,?)', (line,sha,record.id))
            queued = store.enqueue('prepare', record.id, '', {'owner':identity(), 'line':line})
        kick('prepare')
        return jsonify(ok=True, job_pk=record.id, task=queued), 202

    @app.post('/api/editorial/<int:pk>/prepare')
    def editorial_prepare(pk):
        data(); db, source = job(pk); store=Store()
        if store.head(pk): return jsonify(ok=True, ready=True)
        task=store.enqueue('prepare',pk,'',{'owner':identity(),'line':source.line})
        if task['status']=='failed': task=store.retry(task['id'])
        kick('prepare'); return jsonify(ok=True, task=task),202

    @app.get('/api/editorial/<int:pk>')
    def editorial_get(pk):
        job(pk); store=Store(); head=store.head(pk)
        if not head: return jsonify(ok=True, ready=False, tasks=[t for t in store.tasks('prepare') if t['job_pk']==pk])
        info=store.revision(pk,head['revision'])
        cancelled=next((t['payload'].get('cancel_id') for t in store.tasks('publish')
                        if t['job_pk']==pk and t['revision']==head['revision'] and t['status']=='cancelled'),None)
        return jsonify(ok=True,ready=True,draft=info,history=store.history(pk),cancelled_schedule=cancelled)

    @app.put('/api/editorial/<int:pk>')
    def editorial_save(pk):
        body=data(); db,_=job(pk)
        return jsonify(ok=True,draft=service.save_draft(db,Store(),pk,body,identity()))

    @app.post('/api/editorial/<int:pk>/crop')
    def editorial_crop(pk):
        body=data(); db,_=job(pk)
        return jsonify(ok=True,draft=service.crop(db,Store(),pk,body,identity()))

    @app.post('/api/editorial/<int:pk>/restore')
    def editorial_restore(pk):
        body=data(); _,source=job(pk); store=Store()
        info=store.revision(pk,body.get('revision',''))
        result=store.save(pk,source.job_id,source.line,info,expected=body.get('expected_revision'),author=identity())
        return jsonify(ok=True,draft=result)

    @app.get('/api/editorial/<int:pk>/assets/<name>')
    def editorial_asset(pk,name):
        job(pk)
        if not name.endswith(('.jpg','.png','.pdf')): abort(404)
        response=send_file(Store().asset(pk,name),conditional=True)
        response.headers['Cache-Control']='private, max-age=3600'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    @app.get('/api/editorial/<int:pk>/pages/<revision>/<int:page>')
    def editorial_pdf_page(pk,revision,page):
        job(pk); store=Store()
        # 同源、页码和渲染规格的缓存；无需为每次查看重复渲染。
        info=store.revision(pk,revision)
        cache=store.directory(pk)/f'page-{info["source_hash"]}-{page}.json'
        from utils.blog_task_lock import version_lock
        with version_lock(f'{pk}:{page}','editorial_page'):
            if cache.exists(): name=json.loads(cache.read_text())['asset']
            else:
                name=store.put(pk,service.render_page(store,pk,revision,page),'.jpg')
                cache.write_bytes(encode({'asset':name}))
        return send_file(store.asset(pk,name),mimetype='image/jpeg',conditional=True)

    @app.get('/api/editorial/<int:pk>/preview/<revision>/<platform>')
    def editorial_preview(pk,revision,platform):
        db,_=job(pk)
        if platform not in {'wechat','blog'}: abort(404)
        content=service.assemble_preview(db,Store(),pk,revision,platform,lambda name:url_for('editorial_asset',pk=pk,name=name))
        response=app.make_response('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="max-width:760px;margin:24px auto;padding:0 20px">'+content+'</body></html>')
        response.headers['Content-Security-Policy']="default-src 'none'; img-src 'self' https: http:; style-src 'unsafe-inline'; frame-ancestors 'self'; base-uri 'none'"
        response.headers['Cache-Control']='no-store'
        return response

    @app.get('/editorial/template-guide')
    def editorial_guide():
        identity()
        from utils.wechat_template_assets import SOURCE_PDF_GUIDE_PATH
        return send_file(SOURCE_PDF_GUIDE_PATH,mimetype='image/png')

    @app.post('/api/editorial/<int:pk>/publish')
    def editorial_publish(pk):
        body=data(); db,source=job(pk); store=Store()
        if body.get('confirmed') is not True: raise EditorialError('请确认正文、配图、封面和公开发布范围')
        head=store.head(pk)
        if not head or head['revision']!=body.get('revision'): raise Conflict('您预览的版本已变化，请保存并重新预览')
        # 同时检查其他存量后台任务，禁止与旧流程交叉覆盖。
        from utils.auto_blog import AutoBlogTask
        with db.get_session() as session_db:
            active=session_db.query(AutoBlogTask).filter(AutoBlogTask.job_pk==pk,AutoBlogTask.status.in_(('queued','running'))).first()
            if active: raise Conflict('旧版多语言任务尚未完成，请待原任务结束后再提交新版本')
        info,_,_=service.validate_publication(db,store,pk,head['revision'])
        task=store.enqueue('publish',pk,head['revision'],{'reviewer':identity(),'reviewed_at':now(),
             'title':info['title'],'line':source.line,'confirmed':True,'scheduled_for':body.get('scheduled_for'),
             'reschedule_cancel_id':body.get('reschedule_cancel_id'),
             'targets':{key:{'status':'pending'} for key in ['zh','wechat','en','ja','ko','ru']}})
        kick('publish');return jsonify(ok=True,task=task),202

    @app.post('/api/editorial/tasks/<task_id>/cancel-schedule')
    def editorial_cancel_schedule(task_id):
        body=data();store=Store();task=store.task(task_id);job(task['job_pk'])
        return jsonify(ok=True,task=store.cancel_schedule(task_id,author=identity(),schedule_id=body.get('schedule_id')))

    @app.get('/api/editorial/tasks/<task_id>')
    def editorial_task(task_id):
        task=Store().task(task_id);job(task['job_pk'])
        return jsonify(ok=True,task=task)

    @app.get('/api/editorial/tasks/<task_id>/wechat-receipt')
    def editorial_wechat_receipt(task_id):
        """实名、同业务线只读诊断：仅查询已保存的发布号，不提交或修改任务。"""
        task=Store().task(task_id);_,source=job(task['job_pk'])
        target=task['payload'].get('targets',{}).get('wechat',{})
        if task['kind']!='publish' or not target.get('publish_id'):
            raise EditorialError('此任务没有可查询的微信发布回执')
        from batch_processor import _resolve_wechat_account
        from utils.wechat_client import WeChatClient, WeChatAPIError
        try:
            receipt=WeChatClient(account=_resolve_wechat_account(source)).publication_status(target['publish_id'])
        except WeChatAPIError:
            raise EditorialError('微信发布回执查询暂未完成，请稍后再查') from None
        return jsonify(ok=True,receipt={key:receipt.get(key) for key in
                       ('publish_status','article_id','article_detail','fail_idx')})

    @app.post('/api/editorial/tasks/<task_id>/retry')
    def editorial_retry(task_id):
        body=data();store=Store();task=store.task(task_id);job(task['job_pk'])
        task=store.retry(task_id,body.get('target'));kick(task['kind'])
        return jsonify(ok=True,task=task),202
