from flask import Flask, render_template, redirect, g, request, url_for, Response
from models.query import Query
from models.queryrevision import QueryRevision
from models.queryrun import QueryRun
from models.star import Star
from results import SQLiteResultReader
from utils import json_formatter, slugify
# This just provides the translit/long codec, unused otherwise
import translitcodec  # NOQA
import json
import yaml
import output
import os
from sqlalchemy import desc, func
from redissession import RedisSessionInterface
from connections import Connections
from utils.pagination import RangeBasedPagination
import worker

from login import auth
from user import user_blueprint, get_user
from webhelpers import templatehelpers

__dir__ = os.path.dirname(__file__)

app = Flask(__name__)
app.config.update(yaml.load(open(os.path.join(__dir__, "../default_config.yaml"))))
try:
    app.config.update(yaml.load(open(os.path.join(__dir__, "../config.yaml"))))
except IOError:
    # Is ok if we can't load config.yaml
    pass
app.config['DEBUG'] = True

app.register_blueprint(auth)
app.register_blueprint(user_blueprint)
app.register_blueprint(templatehelpers)

app.session_interface = RedisSessionInterface()


class QueriesRangeBasedPagination(RangeBasedPagination):
    def get_page_link(self, page_key, limit):
        get_params = dict(request.args)
        get_params.update({
            'from': page_key, 'limit': limit})
        return url_for('query_runs_all', **dict(
            [(key, value) for key, value in get_params.items()])
        )

    def order_queryset(self):
        if self.direction == 'next':
            self.queryset = self.queryset.order_by(desc(QueryRun.timestamp))
        else:
            self.queryset = self.queryset.order_by(QueryRun.timestamp)

    def filter_queryset(self):
        if self.page_key is None:
            return
        from_query = g.conn.session.query(Query).get(self.page_key)
        if from_query:
            from_qrun_id = from_query.latest_rev.latest_run.id
            if self.direction == 'prev':
                self.queryset = self.queryset.filter(
                    QueryRun.id > from_qrun_id)
            else:
                self.queryset = self.queryset.filter(
                    QueryRun.id < from_qrun_id)


@app.before_request
def setup_context():
    g.conn = Connections(app.config)


@app.teardown_request
def kill_context(exception=None):
    g.conn.close_all()


@app.route("/")
def index():
    return render_template("landing.html", user=get_user())


@app.route("/api/query/unstar", methods=["POST"])
def unstar_query():
    if get_user() is None:
        return "Unauthorized access", 403
    query = g.conn.session.query(Query).get(request.form['query_id'])
    if query:
        star = g.conn.session.query(Star)\
            .filter(Star.query_id == request.form['query_id'])\
            .filter(Star.user_id == get_user().id)\
            .one()
        g.conn.session.delete(star)
        g.conn.session.commit()
        return ""
    else:
        return "Query not found", 404


@app.route("/api/query/star", methods=["POST"])
def star_query():
    if get_user() is None:
        return "Unauthorized access", 403
    query = g.conn.session.query(Query).get(request.form['query_id'])
    if query:
        star = Star()
        star.user = get_user()
        star.query = query
        g.conn.session.add(star)
        g.conn.session.commit()
        return ""
    else:
        return "Query not found", 404


@app.route("/query/new")
def new_query():
    if get_user() is None:
        return redirect("/login?next=/query/new")
    query = Query()
    query.user = get_user()
    g.conn.session.add(query)
    g.conn.session.commit()
    return redirect(url_for('query_show', query_id=query.id))


@app.route("/fork/<int:id>")
def fork_query(id):
    if get_user() is None:
        return redirect("/login?next=fork/{id}".format(id=id))
    query = Query()
    query.user = get_user()
    parent_query = g.conn.session.query(Query).filter(Query.id == id).one()
    query.title = parent_query.title
    query.parent_id = parent_query.id
    query.description = parent_query.description
    g.conn.session.add(query)
    g.conn.session.commit()

    query_rev = QueryRevision(query_id=query.id, text=parent_query.latest_rev.text)
    query.latest_rev = query_rev
    g.conn.session.add(query)
    g.conn.session.add(query_rev)
    g.conn.session.commit()
    return redirect(url_for('query_show', query_id=query.id))


@app.route("/query/<int:query_id>")
def query_show(query_id):
    query = g.conn.session.query(Query).filter(Query.id == query_id).one()
    can_edit = get_user() is not None and get_user().id == query.user_id
    is_starred = False
    if get_user():
        is_starred = g.conn.session.query(func.count(Star.id))\
            .filter(Star.user_id == get_user().id)\
            .filter(Star.query_id == query_id).scalar() == 1
    jsvars = {
        'query_id': query.id,
        'can_edit': can_edit,
        'is_starred': is_starred,
        'published': query.published
    }

    if query.latest_rev and query.latest_rev.latest_run_id:
        jsvars['qrun_id'] = query.latest_rev.latest_run_id

    return render_template(
        "query/view.html",
        user=get_user(),
        query=query,
        jsvars=jsvars,
        latest_rev=query.latest_rev
    )


@app.route('/query/<int:query_id>/result/latest/<string:resultset_id>/<string:format>')
def query_output_redirect(query_id, resultset_id, format):
    query = g.conn.session.query(Query).filter(Query.id == query_id).one()
    qrun_id = query.latest_rev.latest_run_id
    # FIXME: Enforce HTTPS everywhere in a nicer way!
    resp = redirect(
        url_for('output_result', qrun_id=qrun_id,
                resultset_id=resultset_id, format=format,
                _external=True, _scheme='https')
    )
    # CORS on the redirect
    resp.headers.add('Access-Control-Allow-Origin', '*')
    return resp


@app.route('/api/query/meta', methods=['POST'])
def api_set_meta():
    if get_user() is None:
        return "Authentication required", 401
    query = g.conn.session.query(Query).filter(Query.id == request.form['query_id']).one()
    if 'title' in request.form:
        query.title = request.form['title']
    if 'published' in request.form:
        query.published = request.form['published'] == '1'
    if 'description' in request.form:
        query.description = request.form['description']
    g.conn.session.add(query)
    g.conn.session.commit()
    return json.dumps({'id': query.id})


@app.route('/api/query/run', methods=['POST'])
def api_run_query():
    if get_user() is None:
        return "Authentication required", 401
    text = request.form['text']
    query = g.conn.session.query(Query).filter(Query.id == request.form['query_id']).one()

    if query.user_id != get_user().id:
        return "Authorization denied", 403

    if query.latest_rev and query.latest_rev.latest_run:
        result = worker.run_query.AsyncResult(query.latest_rev.latest_run.task_id)
        if not result.ready():
            result.revoke(terminate=True)
            query.latest_rev.latest_run.status = QueryRun.STATUS_SUPERSEDED
            g.conn.session.add(query.latest_rev.latest_run)
            g.conn.session.commit()

    query_rev = QueryRevision(query_id=query.id, text=text)
    query.latest_rev = query_rev

    # XXX (phuedx, 2014/08/08): This deviates from the pre-existing
    # QueryRevision interface, but I'm not confident that SQLAlchemy would
    # invalidate a cached result for a relationship if a property changed.
    query_run = QueryRun()
    query_run.rev = query_rev
    query_run.status = QueryRun.STATUS_QUEUED

    g.conn.session.add(query_run)
    g.conn.session.add(query)
    g.conn.session.commit()
    query_rev.latest_run = query_run
    query_run.task_id = worker.run_query.delay(query_run.id).task_id
    g.conn.session.add(query_rev)
    g.conn.session.add(query_run)
    g.conn.session.commit()
    return json.dumps({
        'qrun_id': query_run.id
    })


@app.route("/query/runs/all")
def query_runs_all():
    queries = g.conn.session.query(Query)\
        .join(Query.latest_rev).join(QueryRevision.latest_run)
    queries_filter = 'all'
    if request.args.get('published') == 'true':
        queries = queries.filter(Query.published)
        queries_filter = 'published'
    limit = int(request.args.get(
        'limit', app.config.get('QUERY_RESULTS_PER_PAGE', 50)))
    queries, prev_link, next_link = QueriesRangeBasedPagination(
        queries, request.args.get('from'), limit,
        request.path,
        request.referrer, dict(request.args)).paginate()
    return render_template(
        "query/list.html", user=get_user(), queries=queries,
        prev_link=prev_link, next_link=next_link,
        queries_filter=queries_filter)


@app.route('/run/<int:qrun_id>/status')
def run_status(qrun_id):
    qrun = g.conn.session.query(QueryRun).get(qrun_id)
    return Response(json.dumps({
        'status': qrun.status_message,
        'extra': json.loads(qrun.extra_info or "{}")
    }), mimetype='application/json', headers={'Access-Control-Allow-Origin': '*'})


@app.route("/run/<int:qrun_id>/output/<int:resultset_id>/<string:format>")
def output_result(qrun_id, resultset_id=0, format='json'):
    qrun = g.conn.session.query(QueryRun).get(qrun_id)
    reader = SQLiteResultReader(qrun, app.config['OUTPUT_PATH_TEMPLATE'])
    response = output.get_formatted_response(format, qrun, reader, resultset_id)
    if request.args.get('download', 'false') == 'true':
        # Download this!
        if qrun.rev.query.title:
            query_name = qrun.rev.query.title
        else:
            query_name = 'untitled'
        filename = "quarry-%s-%s-run%s.%s" % (
            qrun.rev.query.id,
            slugify(query_name),
            qrun.id,
            format
        )
        response.headers['Content-Disposition'] = 'attachment; filename="%s"' % filename.encode('utf-8')
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


@app.route("/run/<int:qrun_id>/meta")
def output_run_meta(qrun_id):
    qrun = g.conn.session.query(QueryRun).get(qrun_id)

    return Response(json.dumps(
        {
            'run': qrun,
            'rev': qrun.rev,
            'query': qrun.rev.query
        }, default=json_formatter),
        mimetype='application/json',
        headers={'Access-Control-Allow-Origin': '*'},
    )


@app.route("/rev/<int:rev_id>/meta")
def output_rev_meta(rev_id):
    rev = g.conn.session.query(QueryRevision).get(rev_id)

    return Response(json.dumps(
        {
            'latest_run': rev.latest_run,
            'rev': rev,
            'query': rev.query
        }, default=json_formatter),
        mimetype='application/json',
        headers={'Access-Control-Allow-Origin': '*'},
    )


@app.route("/query/<int:query_id>/meta")
def output_query_meta(query_id):
    query = g.conn.session.query(Query).get(query_id)

    return Response(json.dumps(
        {
            'latest_run': query.latest_rev.latest_run,
            'latest_rev': query.latest_rev,
            'query': query
        }, default=json_formatter),
        mimetype='application/json',
        headers={'Access-Control-Allow-Origin': '*'},
    )


@app.route("/explain/<int:connection_id>")
def output_explain(connection_id):
    cur = g.conn.replica.cursor()
    try:
        cur.execute('SHOW EXPLAIN FOR %d;' % connection_id)
    except cur.InternalError as e:
        if e[0] in [1094, 1915, 1933]:
            # 1094 = Unknown thread id
            # 1915, 1933 = Target is not running an EXPLAINable command
            return Response(json.dumps(
                {
                    'headers': ['Error'],
                    'rows': [['Hmm... Is the SQL actually running?!']],
                }, default=json_formatter),
                mimetype='application/json',
            )
        else:
            raise
    else:
        return Response(json.dumps(
            {
                'headers': [c[0] for c in cur.description],
                'rows': cur.fetchall(),
            }, default=json_formatter),
            mimetype='application/json',
        )


if __name__ == '__main__':
    app.run(port=5000, host="0.0.0.0")
