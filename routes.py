from flask import render_template, url_for, redirect, request, g,\
                  jsonify, make_response
from datetime import datetime, timedelta, timezone
import lib.twitter
import lib.mastodon
from lib.auth import require_auth, require_auth_api, csrf,\
                     set_session_cookie, get_viewer_session, get_viewer
from model import Session, TwitterArchive, MastodonApp, MastodonInstance,\
                  Account
from app import app, db, sentry, limiter
import tasks
from zipfile import BadZipFile
from twitter import TwitterError
from urllib.error import URLError
import version
import lib.version
import lib.settings
import lib.json
import re


@app.before_request
def load_viewer():
    g.viewer = get_viewer_session()
    if g.viewer and sentry:
        sentry.user_context({
                'id': g.viewer.account.id,
                'username': g.viewer.account.screen_name,
                'service': g.viewer.account.service
            })


@app.context_processor
def inject_version():
    return dict(
            version=version.version,
            repo_url=lib.version.url_for_version(version.version),
        )


@app.context_processor
def inject_sentry():
    if sentry:
        return dict(sentry=True)
    return dict()


@app.after_request
def touch_viewer(resp):
    if 'viewer' in g and g.viewer:
        set_session_cookie(g.viewer, resp, app.config.get('HTTPS'))
        g.viewer.touch()
        db.session.commit()
    return resp


@app.errorhandler(404)
def not_found(e):
    return (render_template('404.html', e=e), 404)


@app.errorhandler(500)
def internal_server_error(e):
    return (render_template('500.html', e=e), 500)


@app.route('/')
def index():
    if g.viewer:
        return render_template(
                'logged_in.html',
                scales=lib.interval.SCALES,
                tweet_archive_failed='tweet_archive_failed' in request.args,
                settings_error='settings_error' in request.args,
                viewer_json=lib.json.account(get_viewer()),
                )
    else:
        instances = (
                MastodonInstance.query
                .filter(MastodonInstance.popularity > 13)
                .order_by(db.desc(MastodonInstance.popularity),
                          MastodonInstance.instance)
                .limit(5))
        return render_template(
                'index.html',
                mastodon_instances=instances,
                twitter_login_error='twitter_login_error' in request.args)


@app.route('/login/twitter')
@limiter.limit('3/minute')
def twitter_login_step1():
    try:
        return redirect(lib.twitter.get_login_url(
            callback=url_for('twitter_login_step2', _external=True),
            **app.config.get_namespace("TWITTER_")
            ))
    except (TwitterError, URLError):
        if sentry:
            sentry.captureException()
        return redirect(
                url_for('index', twitter_login_error='', _anchor='log_in'))


def login(account_id):
    session = Session(account_id=account_id)
    db.session.add(session)
    db.session.commit()

    session.account.dormant = False
    db.session.commit()

    tasks.fetch_acc.s(account_id).apply_async(routing_key='high')

    return session


@app.route('/login/twitter/callback')
@limiter.limit('3/minute')
def twitter_login_step2():
    try:
        oauth_token = request.args['oauth_token']
        oauth_verifier = request.args['oauth_verifier']
        token = lib.twitter.receive_verifier(
                oauth_token, oauth_verifier,
                **app.config.get_namespace("TWITTER_"))

        session = login(token.account_id)

        g.viewer = session
        return redirect(url_for('index'))
    except (TwitterError, URLError):
        if sentry:
            sentry.captureException()
        return redirect(
                url_for('index', twitter_login_error='', _anchor='log_in'))


class TweetArchiveEmptyException(Exception):
    pass


@app.route('/upload_tweet_archive', methods=('POST',))
@limiter.limit('10/10 minutes')
@require_auth
def upload_tweet_archive():
    ta = TwitterArchive(
            account=g.viewer.account,
            body=request.files['file'].read())
    db.session.add(ta)
    db.session.commit()

    try:
        files = lib.twitter.chunk_twitter_archive(ta.id)

        ta.chunks = len(files)
        db.session.commit()

        if not ta.chunks > 0:
            raise TweetArchiveEmptyException()

        for filename in files:
            tasks.import_twitter_archive_month.s(ta.id, filename).apply_async()

        return redirect(url_for('index', _anchor='recent_archives'))
    except (BadZipFile, TweetArchiveEmptyException):
        if sentry:
            sentry.captureException()
        return redirect(
                url_for('index', tweet_archive_failed='',
                        _anchor='tweet_archive_import'))


@app.route('/settings', methods=('POST',))
@csrf
@require_auth
def settings():
    viewer = get_viewer()
    try:
        for attr in lib.settings.attrs:
            if attr in request.form:
                setattr(viewer, attr, request.form[attr])
        db.session.commit()
    except ValueError:
        if sentry:
            sentry.captureException()
        return 400

    return redirect(url_for('index', settings_saved=''))


@app.route('/disable', methods=('POST',))
@csrf
@require_auth
def disable():
    g.viewer.account.policy_enabled = False
    db.session.commit()

    return redirect(url_for('index'))


@app.route('/enable', methods=('POST',))
@csrf
@require_auth
def enable():
    if 'confirm' not in request.form and not g.viewer.account.policy_enabled:
        if g.viewer.account.policy_delete_every == timedelta(0):
            approx = g.viewer.account.estimate_eligible_for_delete()
            return render_template(
                'warn.html',
                message=f"""
                    You've set the time between deleting posts to 0. Every post
                    that matches your expiration rules will be deleted within
                    minutes.
                    { ("That's about " + str(approx) + " posts.") if approx > 0
                        else "" }
                    Go ahead?
                    """)
        if (not g.viewer.account.last_delete or
           g.viewer.account.last_delete <
           datetime.now(timezone.utc) - timedelta(days=365)):
            return render_template(
                    'warn.html',
                    message="""
                        Once you enable Forget, posts that match your
                        expiration rules will be deleted <b>permanently</b>.
                        We can't bring them back. Make sure that you won't
                        miss them.
                        """)

    g.viewer.account.policy_enabled = True
    db.session.commit()

    return redirect(url_for('index'))


@app.route('/logout')
@require_auth
def logout():
    if(g.viewer):
        db.session.delete(g.viewer)
        db.session.commit()
        g.viewer = None
    return redirect(url_for('index'))


@app.route('/api/settings', methods=('PUT',))
@require_auth_api
def api_settings_put():
    viewer = get_viewer()
    data = request.json
    updated = dict()
    for key in lib.settings.attrs:
        if key in data:
            setattr(viewer, key, data[key])
            updated[key] = data[key]
    db.session.commit()
    return jsonify(status='success', updated=updated)


@app.route('/api/viewer')
@require_auth_api
def api_viewer():
    viewer = get_viewer()
    resp = make_response(lib.json.account(viewer))
    resp.headers.set('content-type', 'application/json')
    return resp


@app.route('/login/mastodon', methods=('GET', 'POST'))
def mastodon_login_step1(instance=None):

    instance_url = (request.args.get('instance_url', None)
                    or request.form.get('instance_url', None))

    if not instance_url:
        instances = (
            MastodonInstance
            .query.filter(MastodonInstance.popularity > 1)
            .order_by(db.desc(MastodonInstance.popularity),
                      MastodonInstance.instance)
            .limit(30))
        return render_template(
                'mastodon_login.html', instances=instances,
                address_error=request.method == 'POST',
                generic_error='error' in request.args
                )

    instance_url = instance_url.lower()
    # strip protocol
    instance_url = re.sub('^https?://', '', instance_url,
                          count=1, flags=re.IGNORECASE)
    # strip username
    instance_url = instance_url.split("@")[-1]
    # strip trailing path
    instance_url = instance_url.split('/')[0]

    callback = url_for('mastodon_login_step2',
                       instance_url=instance_url, _external=True)

    try:
        app = lib.mastodon.get_or_create_app(
                instance_url,
                callback,
                url_for('index', _external=True))
        db.session.merge(app)

        db.session.commit()

        return redirect(lib.mastodon.login_url(app, callback))

    except Exception:
        if sentry:
            sentry.captureException()
        return redirect(url_for('mastodon_login_step1', error=True))


@app.route('/login/mastodon/callback/<instance_url>')
def mastodon_login_step2(instance_url):
    code = request.args.get('code', None)
    app = MastodonApp.query.get(instance_url)
    if not code or not app:
        return redirect(url_for('mastodon_login_step1', error=True))

    callback = url_for('mastodon_login_step2',
                       instance_url=instance_url, _external=True)

    token = lib.mastodon.receive_code(code, app, callback)
    account = token.account

    session = login(account.id)

    instance = MastodonInstance(instance=instance_url)
    instance = db.session.merge(instance)
    instance.bump()

    db.session.commit()

    g.viewer = session
    return redirect(url_for('index'))


@app.route('/sentry/setup.js')
def sentry_setup():
    client_dsn = app.config.get('SENTRY_DSN').split('@')
    client_dsn[:1] = client_dsn[0].split(':')
    client_dsn = ':'.join(client_dsn[0:2]) + '@' + client_dsn[3]
    resp = make_response(render_template(
        'sentry.js', sentry_dsn=client_dsn))
    resp.headers.set('content-type', 'text/javascript')
    return resp


@app.route('/dismiss', methods={'POST'})
@csrf
@require_auth
def dismiss():
    get_viewer().reason = None
    db.session.commit()
    return redirect(url_for('index'))


@app.route('/api/reason', methods={'DELETE'})
@require_auth_api
def delete_reason():
    get_viewer().reason = None
    db.session.commit()
    return jsonify(status='success')


@app.route('/api/badge/users')
def users_badge():
    count = (
        Account.query.filter(Account.policy_enabled)
        .filter(~Account.dormant)
        .count()
        )
    return redirect(
            "https://img.shields.io/badge/active%20users-{}-blue.svg"
            .format(count))
