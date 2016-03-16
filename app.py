#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import os
import re
import time
from zipfile import ZipFile

import flask
import flask.ext.pymongo
import pymongo
from slacker import Slacker

import credentials  # local deploy settings

SLACK_TEAM_ID = 'T064J5B38'
SLACK_CLIENT_ID = '6154181110.20526331843'
LOGIN_LINK = ('https://slack.com/oauth/authorize?team=' + SLACK_TEAM_ID +
              '&client_id=' + SLACK_CLIENT_ID + '&scope=identify')
TOKEN_LINK = ('https://slack.com/oauth/authorize?team=' + SLACK_TEAM_ID +
              '&client_id=' + SLACK_CLIENT_ID + '&scope=identify,' +
              'channels:history,channels:read,files:read,'
              'groups:history,groups:read,im:history,im:read,users:read')

app = flask.Flask(__name__)
# TODO eliminate MongoLab mentions
app.config['MONGO_URI'] = os.environ['MONGOLAB_URI']
mongo = flask.ext.pymongo.PyMongo(app)


def is_local_deploy():
    return 'LOCAL' == os.environ.get('PORT', 'LOCAL')


def is_production_deploy():
    return '1' == os.environ.get('PRODUCTION', '0')


def redirect_to_https():
    if 'http' == flask.request.headers.get('X-Forwarded-Proto', 'https'):
        url = flask.request.url.replace('http://', 'https://', 1)
        return flask.redirect(url, code=301)


def redirect_msg(url, msg):
    return flask.render_template('redirect.htm', url_to=url, message=msg)


def username(user):
    return '@'+mongo.db.users.find_one(user[1:])['login']


def streamname(stream):
    return '#'+mongo.db.streams.find_one(stream[1:])['name']


def parse_link(m):
    def link_page(url, title):
        if not title:
            title = url
        return wrap_html('<a href="'+url+'">'+title+'</a>')

    def link_user(user, title):
        return link_page('javascript:void(0)', username(user))

    def link_stream(stream, title):
        return link_page('javascript:void(0)', streamname(stream))

    target = m.group(1)
    label = m.group(2)
    if target.startswith('@'):
        return link_user(target, label)
    if target.startswith('#'):
        return link_stream(target, label)
    if target.startswith('!'):
        return link_page('javascript:void(0)', '@'+target[1:])
    if target.startswith('http'):
        return link_page(target, label)
    return m.group(0)


def wrap_html(text):
    return '||['+text+']||'


def re_iter(regexp, replace, s):
    cur, diff = 0, 0
    s_copy = s
    for res in HTML_RE.finditer(s_copy):
        l,h = res.start(), res.end()
        prev = s[cur:diff+l]
        part = re.sub(regexp, replace, prev)
        s = s[:cur] + part + s[diff+l:]
        diff += len(part)-len(prev)
        cur += len(part) + (h-l)
    return s[:cur] + re.sub(regexp, replace, s[cur:])


def markup(regexp, tag, s, code=False):
    def wrap_all(m):
        return wrap_html('<'+tag+'>'+raw_text(m.group(1))+'</'+tag+'>')

    def wrap_tags(m):
        return wrap_html('<'+tag+'>')+m.group(1)+wrap_html('</'+tag+'>')

    return re_iter(regexp, wrap_all if code else wrap_tags, s)


def raw_text(s):
    return s.replace('<', '&#60;')


def use_entities(s):
    return s.replace('<', '&lt;').replace('>', '&gt;')


def restore_html(m):
    return m.group(1).replace('&lt;', '<').replace('&gt;', '>')


HTML_RE = re.compile(r'\|\|\[(.+?)\]\|\|', re.MULTILINE|re.DOTALL)
LINK_RE = re.compile(r'\B<([^|>]+)\|?([^|>]+)?>\B')
QUOT_RE = re.compile(r'^>(.+?)$', re.MULTILINE)
BOLD_RE = re.compile(r'\B\*(.+?)\*\B')
ITAL_RE = re.compile(r'\b_(.+?)_\b')
STRK_RE = re.compile(r'\B~(.+?)~\B')
PREF_RE = re.compile(r'\B```(.+?)```\B', re.MULTILINE|re.DOTALL)
CODE_RE = re.compile(r'\B`(.+?)`\B')


def parse_msg(msg):
    # TODO move parser to separate class with test
    msg = msg.replace('&gt;', '>') # yes it happens, not from user (&amp; then)
    # markup processing
    msg = markup(PREF_RE, 'pre', msg, True)
    msg = markup(CODE_RE, 'code', msg, True)
    msg = markup(QUOT_RE, 'blockquote', msg)
    msg = markup(STRK_RE, 'strike', msg)
    msg = markup(BOLD_RE, 'b', msg)
    msg = markup(ITAL_RE, 'i', msg)
    msg = re_iter(LINK_RE, parse_link, msg)
    # apply entities for sources
    msg = use_entities(msg)
    # restore markup
    msg = re.sub(HTML_RE, restore_html, msg)
    msg = msg.replace('\n', '<br/>')
    return flask.Markup(msg)


@app.route('/<path:filename>')  
def send_file(filename):  
    return flask.send_from_directory(app.static_folder, filename)


@app.route('/')
def index():
    if flask.request.args.get('code') is None and \
       flask.request.cookies.get('token') is None:
        return redirect_msg(LOGIN_LINK, 'Auth required')
    token = flask.request.cookies.get('token')
    if flask.request.cookies.get('token') is None:
        try:
            oauth = Slacker.oauth.access(
                client_id=SLACK_CLIENT_ID,
                client_secret=os.environ['SLACK_SECRET'],
                code=flask.request.args['code']).body
        except Exception:
            oauth = {}
        # TODO check if our team selected
        if oauth.get('ok') is None:
            return redirect_msg(LOGIN_LINK, 'Auth required')
        token = oauth['access_token']
    client = Slacker(token)
    # TODO check exceptions
    user_info = client.auth.test().body
    response = flask.make_response(
        redirect_msg('/browse', 'Auth success'))
    next_year = time.strftime("%a, %d-%b-%Y %T GMT",
                              time.gmtime(time.time()+365*24*60*60))
    mongo.db.logins.insert_one({'_id': time.time(), 
                                'user': user_info['user'],
                                'token': token})
    response.set_cookie('token', token, expires=next_year)
    response.set_cookie('user', user_info['user'], expires=next_year)
    return response


@app.route('/search')
def search():
    if flask.request.cookies.get('token') is None:
        return redirect_msg(LOGIN_LINK, 'Auth required')
    q = flask.request.args.get('q', '')       # query
    s = flask.request.args.get('s', '')       # stream
    c = flask.request.args.get('c', '')       # context
    p = int(flask.request.args.get('p', 0))   # results page
    n = int(flask.request.args.get('n', 100)) # number of results
    mongo.db.search.insert_one({'_id': time.time(), 
                                'user': flask.request.cookies.get('user'),
                                'q': q})
    results = []
    if q == '':
        return flask.render_template('search.htm', **locals())
    condition = {'$text': {'$search': q}}
    if c != '':
        ts = ts_from_message_id(c)
        condition = {'ts': {'$lt': ts+60*60, '$gt': ts-60*60}, 'to': s}
    elif s != '':
        condition = {'$text': {'$search': q}, 'to': s}
    query = mongo.db.messages\
        .find(condition,
              sort=[('ts', pymongo.DESCENDING)],
              skip=p*n,
              limit=n)
    total = query.count()
    users, streams = {}, {}
    query = sorted(tuple(query), key=lambda r: (r['ts'], r['ts_dot']), reverse=True)
    for res in query:
        # resolving externals
        if res['from'] not in users:
            users[res['from']] = mongo.db.users.find_one(res['from'])
        if res['to'] not in streams:
            streams[res['to']] = mongo.db.streams.find_one(res['to'])
        res['from'] = users[res['from']]
        res['to'] = streams[res['to']]
        res['ctx'] = message_id(str(res['ts'])+'.'+str(res['ts_dot']),
                                res['from']['_id'])
        res['ts'] = time.ctime(res['ts'])
        res['msg'] = parse_msg(res['msg'])
        results.append(res)
    return flask.render_template('search.htm', **locals())


@app.route('/browse')
def browse():
    if flask.request.cookies.get('token') is None:
        return redirect_msg(LOGIN_LINK, 'Auth required')
    s = flask.request.args.get('s', '')       # stream
    p = int(flask.request.args.get('p', 0))   # results page
    n = int(flask.request.args.get('n', 1000))# number of results
    mongo.db.browse.insert_one({'_id': time.time(), 
                                'user': flask.request.cookies.get('user'),
                                's': s})
    results = []
    if s == '':
        f = flask.request.args.get('filter', 'active')
        filters = {'all': {}, 'active': {'active': True}, 'archive': {'active': False}, }
        if f not in filters:
            f = 'active'
        channels = list(mongo.db.streams.find(filters[f], sort=[('name', pymongo.ASCENDING)]))
        return flask.render_template('browse.htm', **locals())
    query = mongo.db.messages\
        .find({'to': s}, 
              sort=[('ts', pymongo.DESCENDING)],
              skip=p*n,
              limit=n)
    total = query.count()
    users, streams = {}, {}
    query = sorted(tuple(query), key=lambda r: (r['ts'], r['ts_dot']), reverse=True)
    for res in query:
        # resolving externals
        if res['from'] not in users:
            users[res['from']] = mongo.db.users.find_one(res['from'])
        if res['to'] not in streams:
            streams[res['to']] = mongo.db.streams.find_one(res['to'])
        res['from'] = users[res['from']]
        res['to'] = streams[res['to']]
        res['ts'] = time.ctime(res['ts'])
        res['msg'] = parse_msg(res['msg'])
        results.append(res)
    return flask.render_template('stream.htm', **locals())


def message_id(timestamp, user):
    return timestamp + '/' + user


def ts_from_message_id(msg_id):
    return int(msg_id.split('/')[0].split('.')[0])


@app.route('/import')
def import_zip_thread():
    # TODO add logging around
    with ZipFile('archive.zip') as archive:
        # import users
        with archive.open('users.json') as users_list:
            users = json.loads(users_list.read())
            bulk = mongo.db.users.initialize_ordered_bulk_op()
            for user in users:
                bulk.find({'_id': user['id']}).upsert().update(
                    {'$set': {'name': user['profile']['real_name'],
                              'login': user['name'],
                              'avatar': user['profile']['image_72']}})
            # manual insert for slackbot user
            bulk.find({'_id': 'USLACKBOT'}).upsert().update(
                {'$set': {'name': 'slackbot',
                          'login': 'slackbot',
                          'avatar': 'https://a.slack-edge.com/0180/img/slackbot_72.png'}})
            result = bulk.execute()

        # import channels
        with archive.open('channels.json') as channel_list:
            channels = json.loads(channel_list.read())
            bulk = mongo.db.streams.initialize_ordered_bulk_op()
            for channel in channels:
                pins = []
                if 'pins' in channel:
                    for pin in channel['pins']:
                        msg_id = message_id(pin['id'], pin['user'])
                        pins.append(msg_id)
                bulk.find({'_id': channel['id']}).upsert().update(
                    {'$set': {'name': channel['name'],
                              'type': 0,  # public channel
                              'active': not channel['is_archived'],
                              'topic': channel['topic']['value'],
                              'pins': pins}})
            result = bulk.execute()

            # import messages
            files = filter(lambda n: not n.endswith(os.path.sep), archive.namelist())
            bulk = mongo.db.messages.initialize_ordered_bulk_op()
            for channel in channels:
                chan_name, chan_id = channel['name'], channel['id']
                for filename in filter(lambda n: n.startswith(chan_name+os.path.sep), files):
                    with archive.open(filename) as day_export:
                        msgs = json.loads(day_export.read())
                        for msg in msgs:
                            if msg.get('subtype') is not None:
                                continue
                            msg_id = message_id(msg['ts'], msg['user'])
                            bulk.find({'_id': msg_id}).upsert().update(
                                {'$set': {'ts': int(msg['ts'].split('.')[0]),
                                          'ts_dot': int(msg['ts'].split('.')[1]),
                                          'msg': msg['text'],
                                          'from': msg['user'],
                                          'to': chan_id}})
            result = bulk.execute()
            mongo.db.messages.create_index('ts')
            mongo.db.messages.create_index('to')
            mongo.db.messages.create_index('from')
            mongo.db.messages.create_index([('msg', 'text')], default_language='ru')
    skip_fields = ['upserted', 'modified', 'matched', 'removed', 'inserted']
    for field in skip_fields:
        result.pop(field, None)
    return 'Import complete!<br />' + str(result)

if __name__ == "__main__":
    app.config['PREFERRED_URL_SCHEME'] = 'https'
    app.before_request(redirect_to_https)
    if is_local_deploy():
        app.run(port=8080, debug=True)
    else:
        app.run(host='0.0.0.0', 
                port=int(os.environ['PORT']), 
                debug=not is_production_deploy())
