#!/usr/bin/env python3
# coding: utf-8
# SPDX-License-Identifier: Apache-2.0


# Copyright 2019 AntiCompositeNumber

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#   http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time
import urllib.parse
import uuid
import csv
import requests
import mwparserfromhell
import flask
from fuzzywuzzy import fuzz

bp = flask.Blueprint('citeinspector', __name__, url_prefix='/citeinspector')


class HandledError(Exception):
    """Exception that have been flashed already

    Attributes:
        orig_type -- Original exception type
        message -- Additional message (optional)
        """
    def __init__(self, orig_type, message=None):
        self.orig_type = orig_type
        self.message = message


def flash(message, category="message"):
    try:
        flask.flash(message, category)
    except RuntimeError:
        print(category + ':', message)


def get_retry(url, method='get', output='object', data=None):
    """Make a request for a resource and retry if that doesn't work."""
    headers = {'user-agent': 'anticompositetools/citeinspector '
               '(https://tools.wmflabs.org/anticompositetools/citeinspector; '
               'tools.anticompositetools@tools.wmflabs.org) python-requests/'
               + requests.__version__}

    for i in range(1, 5):
        try:
            if method == 'get':
                response = requests.get(url, headers=headers)
            elif method == 'post':
                response = requests.post(url, headers=headers, data=data)

            response.raise_for_status()

            if output == 'json':
                output_json = response.json()

        except Exception as err:
            print(err)
            if response.status_code == 404:
                if output == 'object':
                    return response
                else:
                    return None
            elif i == 4:
                raise
            else:
                time.sleep(5*i)
                continue

    if output == 'json':
        return output_json
    elif output == 'object':
        return response


def get_wikitext(url):
    wikitext_url = url + '&action=raw'

    request = get_retry(wikitext_url)
    if request.status_code == 404:
        flash('That page does not exist.', 'danger')
        raise HandledError('404 Client Error')

    else:
        start_time = time.strftime('%Y%m%d%H%M%S', time.gmtime())
        timestruct = time.strptime(request.headers['Last-Modified'],
                                   '%a, %d %b %Y %H:%M:%S %Z')
        edit_time = time.strftime('%Y%m%d%H%M%S', timestruct)
        return (request.text, (edit_time, start_time))


def get_citoid_template_types():
    """Loads template to citoid type mapping from wiki"""
    url = get_page_url(
        'MediaWiki:Citoid-template-type-map.json') + '&action=raw'
    template_type_map = get_retry(url=url, output='json')
    supported_templates = [template for key, template
                           in template_type_map.items()
                           if template != 'Citation']

    return template_type_map, supported_templates


def find_refs(code, supported_templates):
    """Find refs in the wikitext"""
    # Check for <ref> tags with citation templates
    for tag in code.ifilter_tags(matches="ref"):
        if tag.contents:
            # Ignore self-closed, unparsable, and empty tags
            cite_data, template_name = grab_cite_data(
                tag.contents.filter_templates()[0], supported_templates)
            if cite_data is not None:
                try:
                    # If the reference is already named, use that.
                    ref_id = tag.get('name').value
                except ValueError:
                    # Otherwise, grab a uuid to use in place of the name
                    ref_id = uuid.uuid4()

                yield dict(name=str(ref_id), template=template_name,
                           source='wikitext', location='ref',
                           wikitext=str(tag), data=cite_data)

    # Check for citation templates elsewhere in the text
    for template in code.ifilter_templates(recursive=False):
        cite_data, template_name = grab_cite_data(template,
                                                  supported_templates)
        if cite_data is not None:
            # We could generate a Harvard anchor here, but I don't trust
            # that to be unique
            ref_id = uuid.uuid4()
            yield dict(name=str(ref_id), template=template_name,
                       source='wikitext', location='text',
                       wikitext=str(template), data=cite_data)


def grab_cite_data(template, supported_templates):
    """Check for supported templates and extract citation data"""
    template_name = str(template.name).lower().strip().capitalize()

    if template_name in supported_templates:
        data = {str(para.name).lower().strip(): str(para.value)
                for para in template.params}
        return data, template_name
    else:
        return None, None


def get_bib_ident(cite_data):
    """Return the best identifier (ISBN, DOI, PMID, PMCID, or URL)"""
    data = cite_data['data']
    return data.get(
        'isbn', data.get(
            'pmcid', data.get(
                'pmid', data.get(
                    'doi', data.get(
                        'url')))))


def get_parsoid_data(ident):
    rest_api = 'https://en.wikipedia.org/api/rest_v1/'
    parsoid_endpoint = 'data/citation/{format}/{query}'.format(
            format='mediawiki', query=ident)

    url = rest_api + parsoid_endpoint
    return get_retry(url, output='json')[0]


def map_parsoid_to_templates(raw_parsoid_data, wikitext_data,
                             templatedata_cache, template_type_map):
    try:
        parsoid_template = template_type_map[raw_parsoid_data["itemType"]]
    except KeyError:
        return None

    try:
        templatedata = templatedata_cache[parsoid_template]
    except KeyError:
        templatedata = get_TemplateData_map(parsoid_template)
        templatedata_cache[parsoid_template] = templatedata
    td_map = templatedata['maps']['citoid']

    def lastnamefirstname(author):
        if author[0] == "":
            parsed = list(csv.reader([author[1]], skipinitialspace=True))[0]
            return parsed[0], parsed[1]
        else:
            return author[1], author[0]

    data = {}
    for key, value in raw_parsoid_data.items():
        if key == "author":
            for i, author in enumerate(value):
                last, first = lastnamefirstname(author)
                data['last' + str(i + 1)] = last
                data['first' + str(i + 1)] = first

        elif key == "editor":
            for i, [first, last] in enumerate(value):
                last, first = lastnamefirstname(author)
                data['editor' + str(i + 1) + '-last']
                data['editor' + str(i + 1) + '-first']

        elif type(value) is str:
            param = td_map.get(key)
            if param is not None:
                data[param] = value
    return dict(name=wikitext_data['name'], template=parsoid_template,
                source=raw_parsoid_data.get('source', '[Citoid]')[0],
                location=wikitext_data['location'], data=data)


def get_TemplateData_map(template):
    mw_api = 'https://en.wikipedia.org/w/api.php'
    request_body = dict(action='templatedata', format='json',
                        titles='Template:' + template)

    templatedata = get_retry(mw_api, method='post', output='json',
                             data=request_body)
    pages = templatedata['pages']
    return pages[list(pages)[0]]


def concat_items(wikitext_data, citoid_data):
    cite = {}
    wt_citedata = wikitext_data['data']
    ct_citedata = citoid_data['data']
    cite['name'] = wikitext_data['name']
    if wikitext_data['template'] == citoid_data['template']:
        cite['template'] = [wikitext_data['template'], citoid_data['template']]
    cite['citoid_source'] = citoid_data['source']
    cite['location'] = wikitext_data['location']
    cite['ratio'] = fuzz_set(wt_citedata.values(), ct_citedata.values())
    cite['data'] = {}

    keys = list(ct_citedata)
    for key in wt_citedata:
        if key not in keys:
            keys.append(key)

    for key in keys:
        wt_value = wt_citedata.get(key, '')
        ct_value = ct_citedata.get(key, '')
        cite['data'][key] = {
            'wikitext': wt_value,
            'citoid': ct_value,
            'ratio': fuzz_item(wt_value, ct_value)
            }

    return cite


def fuzz_item(item_a, item_b):
    return fuzz.partial_ratio(item_a, item_b)


def fuzz_set(set_a, set_b):
    str_a = ''
    str_b = ''
    for item in set_a:
        str_a += item + ' '
    for item in set_b:
        str_b += item + ' '
    return fuzz.token_set_ratio(str_a, str_b)


def get_page_url(rawinput):
    """Take the user input and get a suitable URL out of it.
    If the input is not a URL, assume it's an en.wp page, since only en.wp is
    supported right now.
    """
    parsed = urllib.parse.urlparse(rawinput)

    site = parsed.netloc
    if 'http' not in parsed.scheme:
        # Assume page on enwiki
        title = rawinput
        site = 'en.wikipedia.org'
    elif site != 'en.wikipedia.org':
        flash('Sorry, but only the English Wikipedia is supported right now',
              'danger')
        raise ValueError
    elif parsed.path == '/w/index.php':
        query_params = urllib.parse.parse_qs(parsed.query)
        if 'oldid' not in query_params:
            title = query_params['title'][0]
        else:
            flash('Invalid URL', 'danger')
            raise ValueError  # fix
    elif '/wiki/' in parsed.path:
        title = parsed.path[6:]
    else:
        flash('Invalid URL', 'danger')
        raise ValueError  # this one too

    return 'https://' + site + '/w/index.php?title=' + title


def citeinspector(rawinput):
    url = get_page_url(rawinput)
    wikitext, times = get_wikitext(url)
    template_type_map, supported_templates = get_citoid_template_types()

    templatedata_cache = {}
    output = []
    code = mwparserfromhell.parse(wikitext)

    for old_data in find_refs(code, supported_templates):
        ident = get_bib_ident(old_data)
        raw_parsoid_data = get_parsoid_data(ident)
        parsoid_data = map_parsoid_to_templates(
            raw_parsoid_data, old_data, templatedata_cache, template_type_map)
        citedata = concat_items(old_data, parsoid_data)
        output.append(citedata)

    return output


@bp.route('/', methods=['GET'])
def form():
    return flask.render_template('citeinspector.html')


@bp.route('/output', methods=['POST'])
def output():
    pageurl = flask.request.form['page_url']
    output = citeinspector(pageurl)
    return flask.render_template('citeinspector-diff.html', d=output)
