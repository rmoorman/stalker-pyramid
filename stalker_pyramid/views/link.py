# -*- coding: utf-8 -*-
# Stalker Pyramid a Web Base Production Asset Management System
# Copyright (C) 2009-2014 Erkan Ozgur Yilmaz
#
# This file is part of Stalker Pyramid.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation;
# version 2.1 of the License.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
import shutil
import subprocess
import tempfile

import os
import logging
import uuid
import base64
import transaction
from HTMLParser import HTMLParser
from PIL import Image

from pyramid.response import Response, FileResponse
from pyramid.view import view_config
from pyramid.httpexceptions import HTTPOk

from stalker.db import DBSession
from stalker import Entity, Link, defaults, Repository, Version

from stalker_pyramid.views import (get_logged_in_user, get_multi_integer,
                                   get_tags, StdErrToHTMLConverter)


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class ImageData(object):
    """class for handling image data coming from html
    """

    def __init__(self, data):
        self.raw_data = data
        self.type = ''
        self.extension = ''
        self.base64_data = ''
        self.parse()

    def parse(self):
        """parses the data
        """
        temp_data = self.raw_data.split(';')
        self.type = temp_data[0].split(':')[1]
        self.extension = '.%s' % self.type.split('/')[1]
        self.base64_data = temp_data[1].split(',')[1]


class ImgToLinkConverter(HTMLParser):
    """An HTMLParser derivative that parses HTML data and replaces the ``src``
    attributes in <img> tags with Link paths
    """

    def __init__(self):
        HTMLParser.__init__(self)
        self.raw_img_to_url = []
        self.links = []
        self.raw_data = ''

    def feed(self, data):
        """the overridden feed method which stores the original data
        """
        HTMLParser.feed(self, data)
        self.raw_data = data

    def handle_starttag(self, tag, attrs):
        # print tag, attrs
        attrs_dict = {}
        if tag == 'img':
            # convert attributes to a dict
            for attr in attrs:
                attrs_dict[attr[0]] = attr[1]
            src = attrs_dict['src']

            # check if it contains data
            if not src.startswith('data'):
                return

            # get the file type and use it as extension
            image_data = ImageData(src)
            # generate a path for this file
            file_full_path = \
                MediaManager.generate_local_file_path(image_data.extension)
            link_full_path = \
                MediaManager.convert_full_path_to_file_link(file_full_path)
            original_name = os.path.basename(file_full_path)

            # create folders
            try:
                os.makedirs(os.path.dirname(file_full_path))
            except OSError:
                # path exists
                pass

            with open(file_full_path, 'wb') as f:
                f.write(
                    base64.decodestring(image_data.base64_data)
                )

            # create Link instances
            # create a Link instance and return it
            new_link = Link(
                full_path=link_full_path,
                original_filename=original_name,
            )
            DBSession.add(new_link)
            self.links.append(new_link)

            # save data to be replaced in the raw content
            self.raw_img_to_url.append(
                (src, link_full_path)
            )

    def replace_urls(self):
        """replaces the raw image data with the url in the given data
        """
        for img_to_url in self.raw_img_to_url:
            self.raw_data = self.raw_data.replace(
                img_to_url[0],
                '/%s' % img_to_url[1]
            )
        return self.raw_data


def replace_img_data_with_links(raw_data):
    """replaces the image data coming in base64 form with Links

    :param raw_data: The raw html data that may contain <img> elements
    :returns str, list: string containing html data with the ``src`` parameters
      of <img> tags are replaced with Link addresses and the generated links
    """
    parser = ImgToLinkConverter()
    parser.feed(raw_data)
    parser.replace_urls()
    return parser.raw_data, parser.links


@view_config(
    route_name='upload_files',
    renderer='json'
)
def upload_files(request):
    """uploads a list of files to the server, creates Link instances in server
    and returns the created link ids with a response to let the front end
    request a linkage between the entity and the uploaded files
    """
    # decide if it is single or multiple files
    file_params = request.POST.getall('file')
    logger.debug('file_params: %s ' % file_params)

    new_links = []
    try:
        new_links_info = MediaManager.upload_files(file_params)

        for new_link_info in new_links_info:
            new_link_info.update({'created_by': get_logged_in_user(request)})
            new_link = Link(**new_link_info)
            new_links.append(new_link)

        # to get link.ids now, we need to do a commit
        DBSession.add_all(new_links)
        transaction.commit()
    except IOError as e:
        c = StdErrToHTMLConverter(e)
        response = Response(c.html())
        response.status_int = 500
        transaction.abort()
        return response
    else:
        # add them to thd DBSession again
        # store the link object
        DBSession.add_all(new_links)

        logger.debug('created links for uploaded files: %s' % new_links)

        return {
            'link_ids': [link.id for link in new_links]
        }


@view_config(
    route_name='assign_thumbnail',
)
def assign_thumbnail(request):
    """assigns the thumbnail to the given entity
    """
    link_ids = get_multi_integer(request, 'link_ids[]')
    entity_id = request.params.get('entity_id', -1)

    link = Link.query.filter(Link.id.in_(link_ids)).first()
    entity = Entity.query.filter_by(id=entity_id).first()

    logger.debug('link_ids  : %s' % link_ids)
    logger.debug('link      : %s' % link)
    logger.debug('entity_id : %s' % entity_id)
    logger.debug('entity    : %s' % entity)

    logged_in_user = get_logged_in_user(request)
    if entity and link:
        entity.thumbnail = link

        # resize the thumbnail
        file_full_path = MediaManager.convert_file_link_to_full_path(link.full_path)
        img = Image.open(file_full_path)
        if img.format != 'GIF':
            img.thumbnail((1024, 1024))
            img.thumbnail((512, 512), Image.ANTIALIAS)
            img.save(file_full_path)

        DBSession.add(entity)
        DBSession.add(link)

    return HTTPOk()


@view_config(
    route_name='assign_reference',
    renderer='json'
)
def assign_reference(request):
    """assigns the link to the given entity as a new reference
    """
    link_ids = get_multi_integer(request, 'link_ids[]')
    removed_link_ids = get_multi_integer(request, 'removed_link_ids[]')
    entity_id = request.params.get('entity_id', -1)

    entity = Entity.query.filter_by(id=entity_id).first()
    links = Link.query.filter(Link.id.in_(link_ids)).all()
    removed_links = Link.query.filter(Link.id.in_(removed_link_ids)).all()

    # Tags
    tags = get_tags(request)

    logged_in_user = get_logged_in_user(request)

    logger.debug('link_ids      : %s' % link_ids)
    logger.debug('links         : %s' % links)
    logger.debug('entity_id     : %s' % entity_id)
    logger.debug('entity        : %s' % entity)
    logger.debug('tags          : %s' % tags)
    logger.debug('removed_links : %s' % removed_links)

    # remove all the removed links
    for removed_link in removed_links:
        # no need to search for any linked tasks here
        DBSession.delete(removed_link)

    if entity and links:
        entity.references.extend(links)

        # assign all the tags to the links
        for link in links:
            # add the ones not in the tags already
            for tag in tags:
                if tag not in link.tags:
                    link.tags.append(tag)
            #link.tags.extend(tags)
            # generate thumbnail
            thumbnail = MediaManager.generate_thumbnail(link)
            link.thumbnail = thumbnail
            thumbnail.created_by = logged_in_user
            DBSession.add(thumbnail)

        DBSession.add(entity)
        DBSession.add_all(links)

    # return new links as json data
    # in response text
    return [
        {
            'id': link.id,
            'full_path': link.full_path,
            'original_filename': link.original_filename,
            'thumbnail_full_path': link.thumbnail.full_path
            if link.thumbnail else link.full_path,
            'tags': [tag.name for tag in link.tags]
        } for link in links
    ]


@view_config(route_name='get_project_references', renderer='json')
@view_config(route_name='get_task_references', renderer='json')
@view_config(route_name='get_asset_references', renderer='json')
@view_config(route_name='get_shot_references', renderer='json')
@view_config(route_name='get_sequence_references', renderer='json')
@view_config(route_name='get_entity_references', renderer='json')
def get_entity_references(request):
    """called when the references to Project/Task/Asset/Shot/Sequence is
    requested
    """
    entity_id = request.matchdict.get('id', -1)
    entity = Entity.query.filter(Entity.id == entity_id).first()
    logger.debug('asking references for entity: %s' % entity)

    offset = request.params.get('offset', 0)
    limit = request.params.get('limit', 1e10)

    search_string = request.params.get('search', '')
    logger.debug('search_string: %s' % search_string)

    search_query = ''
    if search_string != "":
        search_string_buffer = ['and (']
        for i, s in enumerate(search_string.split(' ')):
            if i != 0:
                search_string_buffer.append('or')
            tmp_search_query = """
            '%(search_str)s' = any (entity_tags.tags)
            or tasks.entity_type = '%(search_str)s'
            or tasks.full_path ilike '%(search_wide)s'
            or "Links".original_filename ilike '%(search_wide)s'
            """ % {
                'search_str': s,
                'search_wide': '%{s}%'.format(s=s)
            }
            search_string_buffer.append(tmp_search_query)
        search_string_buffer.append(')')
        search_query = '\n'.join(search_string_buffer)
    logger.debug('search_query: %s' % search_query)

    # we need to do that import here
    from stalker_pyramid.views.task import \
        query_of_tasks_hierarchical_name_table

    # using Raw SQL queries here to fasten things up quite a bit and also do
    # some fancy queries like getting all the references of tasks of a project
    # also with their tags
    sql_query = """
    -- select all links assigned to a project tasks or assigned to a task and its children
select
    "Links".id,
    "Links".full_path,
    "Links".original_filename,
    "Thumbnails".full_path as "thumbnail_full_path",
    entity_tags.tags,
    array_agg(tasks.id) as entity_id,
    array_agg(tasks.full_path) as full_path,
    array_agg(tasks.entity_type) as entity_type
from (
    %(tasks_hierarchical_name_table)s
) as tasks
join "Task_References" on tasks.id = "Task_References".task_id
join "Links" on "Task_References".link_id = "Links".id
join "SimpleEntities" as "Link_SimpleEntities" on "Links".id = "Link_SimpleEntities".id
join "Links" as "Thumbnails" on "Link_SimpleEntities".thumbnail_id = "Thumbnails".id
left outer join (
    select
        "Links".id,
        array_agg("Tag_SimpleEntities".name) as tags
    from "Links"
    join "Entity_Tags" on "Links".id = "Entity_Tags".entity_id
    join "SimpleEntities" as "Tag_SimpleEntities" on "Entity_Tags".tag_id = "Tag_SimpleEntities".id
    group by "Links".id
) as entity_tags on "Links".id = entity_tags.id

where (%(id)s = any (tasks.path) or tasks.id = %(id)s) %(search_string)s

group by "Links".id,
    "Links".full_path,
    "Links".original_filename,
    "Thumbnails".id,
    entity_tags.tags

order by "Links".id

offset %(offset)s
limit %(limit)s
    """ % {
        'id': entity_id,
        'tasks_hierarchical_name_table':
        query_of_tasks_hierarchical_name_table(),
        'search_string': search_query,
        'offset': offset,
        'limit': limit
    }

    # if offset and limit:
    #     sql_query += "offset %s limit %s" % (offset, limit)

    from sqlalchemy import text  # to be able to use "%" sign use this function
    result = DBSession.connection().execute(text(sql_query))

    return_val = [
        {
            'id': r[0],
            'full_path': r[1],
            'original_filename': r[2],
            'thumbnail_full_path': r[3],
            'tags': r[4],
            'entity_ids': r[5],
            'entity_names': r[6],
            'entity_types': r[7]
        } for r in result.fetchall()
    ]

    return return_val


@view_config(route_name='get_project_references_count', renderer='json')
@view_config(route_name='get_task_references_count', renderer='json')
@view_config(route_name='get_asset_references_count', renderer='json')
@view_config(route_name='get_shot_references_count', renderer='json')
@view_config(route_name='get_sequence_references_count', renderer='json')
@view_config(route_name='get_entity_references_count', renderer='json')
def get_entity_references_count(request):
    """called when the count of references to Project/Task/Asset/Shot/Sequence
    is requested
    """
    entity_id = request.matchdict.get('id', -1)
    entity = Entity.query.filter(Entity.id == entity_id).first()
    logger.debug('asking references for entity: %s' % entity)

    search_string = request.params.get('search', '')
    logger.debug('search_string: %s' % search_string)

    search_query = ''
    if search_string != "":
        search_string_buffer = ['and (']
        for i, s in enumerate(search_string.split(' ')):
            if i != 0:
                search_string_buffer.append('or')
            tmp_search_query = """
            '%(search_str)s' = any (entity_tags.tags)
            or tasks.entity_type = '%(search_str)s'
            or tasks.full_path ilike '%(search_wide)s'
            or "Links".original_filename ilike '%(search_wide)s'
            """ % {
                'search_str': s,
                'search_wide': '%{s}%'.format(s=s)
            }
            search_string_buffer.append(tmp_search_query)
        search_string_buffer.append(')')
        search_query = '\n'.join(search_string_buffer)
    logger.debug('search_query: %s' % search_query)

    # we need to do that import here
    from stalker_pyramid.views.task import \
        query_of_tasks_hierarchical_name_table

    # using Raw SQL queries here to fasten things up quite a bit and also do
    # some fancy queries like getting all the references of tasks of a project
    # also with their tags
    sql_query = """
    -- select all links assigned to a project tasks or assigned to a task and its children
select count(1) from (
    select
        "Links".id,
        "Links".full_path,
        "Links".original_filename,
        "Thumbnails".full_path as "thumbnail_full_path",
        entity_tags.tags,
        array_agg(tasks.id) as entity_id,
        array_agg(tasks.full_path) as full_path,
        array_agg(tasks.entity_type) as entity_type
    from (
        %(tasks_hierarchical_name_table)s
    ) as tasks
    join "Task_References" on tasks.id = "Task_References".task_id
    join "Links" on "Task_References".link_id = "Links".id
    join "SimpleEntities" as "Link_SimpleEntities" on "Links".id = "Link_SimpleEntities".id
    join "Links" as "Thumbnails" on "Link_SimpleEntities".thumbnail_id = "Thumbnails".id
    left outer join (
        select
            "Links".id,
            array_agg("Tag_SimpleEntities".name) as tags
        from "Links"
        join "Entity_Tags" on "Links".id = "Entity_Tags".entity_id
        join "SimpleEntities" as "Tag_SimpleEntities" on "Entity_Tags".tag_id = "Tag_SimpleEntities".id
        group by "Links".id
    ) as entity_tags on "Links".id = entity_tags.id

    where (%(id)s = any (tasks.path) or tasks.id = %(id)s) %(search_string)s

    group by "Links".id,
        "Links".full_path,
        "Links".original_filename,
        "Thumbnails".id,
        entity_tags.tags
    ) as data
    """ % {
        'id': entity_id,
        'tasks_hierarchical_name_table':
        query_of_tasks_hierarchical_name_table(),
        'search_string': search_query
    }

    from sqlalchemy import text  # to be able to use "%" sign use this function
    result = DBSession.connection().execute(text(sql_query))

    return result.fetchone()[0]


@view_config(
    route_name='delete_reference',
    permission='Delete_Link'
)
def delete_reference(request):
    """deletes the reference with the given ID
    """
    ref_id = request.matchdict.get('id')
    ref = Link.query.get(ref_id)

    files_to_remove = []
    if ref:
        original_filename = ref.original_filename
        # check if it has a thumbnail
        if ref.thumbnail:
            # remove the file first
            files_to_remove.append(ref.thumbnail.full_path)

            # delete the thumbnail Link from the database
            DBSession.delete(ref.thumbnail)
        # remove the reference itself
        files_to_remove.append(ref.full_path)

        # delete the ref Link from the database
        # IMPORTANT: Because there is no link from Link -> Task deleting a Link
        #            directly will raise an IntegrityError, so remove the Link
        #            from the associated Task before deleting it
        from stalker import Task
        for task in Task.query.filter(Task.references.contains(ref)).all():
            logger.debug('%s is referencing %s, '
                         'breaking this relation' % (task, ref))
            task.references.remove(ref)
        DBSession.delete(ref)

        # now delete files
        for f in files_to_remove:
            # convert the paths to system path
            f_system = MediaManager.convert_file_link_to_full_path(f)
            try:
                os.remove(f_system)
            except OSError:
                pass

        response = Response('%s removed successfully' % original_filename)
        response.status_int = 200
        return response
    else:
        response = Response('No ref with id : %i' % ref_id)
        response.status_int = 500
        transaction.abort()
        return response


@view_config(
    route_name='serve_files'
)
def serve_files(request):
    """serves files in the stalker server side storage
    """
    partial_file_path = request.matchdict['partial_file_path']
    file_full_path = MediaManager.convert_file_link_to_full_path(partial_file_path)
    return FileResponse(file_full_path)


@view_config(
    route_name='forced_download_files'
)
def force_download_files(request):
    """serves files but forces to download
    """
    partial_file_path = request.matchdict['partial_file_path']
    file_full_path = MediaManager.convert_file_link_to_full_path(partial_file_path)
    # get the link to get the original file name
    link = Link.query.filter(
        Link.full_path == 'SPL/' + partial_file_path).first()
    if link:
        original_filename = link.original_filename
    else:
        original_filename = os.path.basename(file_full_path)

    response = FileResponse(
        file_full_path,
        request=request,
        content_type='application/force-download',
    )
    # update the content-disposition header
    response.headers['content-disposition'] = \
        str('attachment; filename=' + original_filename)
    return response


class MediaManager(object):
    """Manages media files.

    MediaManager is the media hub of Stalker Pyramid. It is responsible of the
    uploads/downloads of media files and all kind of conversions.

    It can convert image, video and audio files. The default format for image
    files is PNG and the default format for video os WebM (VP8), and mp3
    (stereo, 96 kBit/s) is the default format for audio files.

    It can filter files from request parameters and upload them to the server,
    also for image files it will generate thumbnails and versions to be viewed
    from web.

    It can handle image sequences, and will create only one Link object per
    image sequence. The thumbnail of an image sequence will be a gif image.

    It will generate a zip file to serve all the images in an image sequence.
    """

    def __init__(self):
        self.reference_path = 'References/Stalker_Pyramid/'
        self.version_output_path = 'Outputs/Stalker_Pyramid/'

        # accepted image formats
        self.image_formats = ['.jpg', '.jpeg', '.gif', '.png', '.tga', '.tif',
                              '.tiff', '.exr', '.bmp']

        # accepted video formats
        self.video_formats = ['.mov', '.avi', '.flv', '.mp4', '.mpg', '.mpeg',
                              '.webm']

        # thumbnail format
        self.thumbnail_format = '.png'
        self.thumbnail_width = 512
        self.thumbnail_height = 512

        # images and videos for web
        self.web_image_format = '.png'
        self.web_image_width = 1920
        self.web_image_height = 1080

        self.web_video_format = '.webm'
        self.web_video_width = 960
        self.web_video_height = 540
        self.web_video_bitrate = 2048  # in kBits/sec

        # commands
        self.ffmpeg_command_path = '/usr/bin/ffmpeg'
        self.ffprobe_command_path = '/usr/local/bin/ffprobe'

    def generate_image_thumbnail(self, file_full_path):
        """Generates a thumbnail for the given image file

        :param file_full_path: Generates a thumbnail for the given file in the
          given path
        :return str: returns the thumbnail path
        """
        # generate thumbnail for the image and save it to a tmp folder
        thumbnail_path = tempfile.mktemp(suffix=self.thumbnail_format)
        img = Image.open(file_full_path)
        img.thumbnail((2 * self.thumbnail_width, 2 * self.thumbnail_height))
        img.thumbnail((self.thumbnail_width, self.thumbnail_height),
                      Image.ANTIALIAS)
        img.save(thumbnail_path)
        return thumbnail_path

    def generate_image_for_web(self, file_full_path):
        """Generates a version suitable to be viewed from a web browser.

        :param file_full_path: Generates a thumbnail for the given file in the
          given path.
        :return str: returns the thumbnail path
        """
        # generate thumbnail for the image and save it to a tmp folder
        thumbnail_path = tempfile.mktemp(suffix=self.thumbnail_format)
        img = Image.open(file_full_path)

        if img.size[0] > self.web_image_width \
           or img.size[1] > self.web_image_height:
            img.thumbnail((2 * self.thumbnail_width,
                           2 * self.thumbnail_height))
            img.thumbnail((self.thumbnail_width, self.thumbnail_height),
                          Image.ANTIALIAS)
        img.save(thumbnail_path)
        return thumbnail_path

    def generate_video_thumbnail(self, file_full_path):
        """Generates a thumbnail for the given video link

        :param str file_full_path: A string showing the full path of the video
          file.
        """
        # TODO: split this in to two different methods, one generating
        #       thumbnails from the video and another one accepting three
        #       images
        video_info = self.get_video_info(file_full_path)
        # get the correct stream
        video_stream = None
        for stream in video_info:
            if stream['codec_type'] == 'video':
                video_stream = stream

        try:
            nb_frames = int(video_stream['nb_frames'])
        except KeyError:
            # no nb_frames
            # generate only from first frame
            nb_frames = 4

        start_thumb_path = tempfile.mktemp(suffix=self.thumbnail_format)
        mid_thumb_path = tempfile.mktemp(suffix=self.thumbnail_format)
        end_thumb_path = tempfile.mktemp(suffix=self.thumbnail_format)

        thumbnail_path = tempfile.mktemp(suffix=self.thumbnail_format)

        # generate three thumbnails from the start, middle and end of the file
        #start_frame
        self.ffmpeg(**{
            'i': file_full_path,
            'vf': "select='eq(n, 0)'",
            'vframes': 1,
            'o': start_thumb_path
        })
        #mid_frame
        self.ffmpeg(**{
            'i': file_full_path,
            'vf': "select='eq(n, %s)'" % (int(nb_frames/2.0)),
            'vframes': 1,
            'o': mid_thumb_path
        })
        #end_frame
        self.ffmpeg(**{
            'i': file_full_path,
            'vf': "select='eq(n, %s)'" % (nb_frames - 2),
            'vframes': 1,
            'o': end_thumb_path
        })

        # now merge them
        self.ffmpeg(**{
            'i': [start_thumb_path, mid_thumb_path, end_thumb_path],
            'filter_complex':
                '[0:0] scale=-1:%(th)s/3, pad=%(tw)s:%(th)s [l]; '
                '[1:0] scale=-1:%(th)s/3, fade=out:300:30:alpha=1 [m]; '
                '[2:0] scale=-1:%(th)s/3, fade=out:300:30:alpha=1 [r]; '
                '[l][m] overlay=0:%(th)s/3 [x]; [x][r] overlay=0:2*%(th)s/3' %
                {
                    'tw': self.thumbnail_width,
                    'th': self.thumbnail_height
                },
            'o': thumbnail_path
        })

        # remove the intermediate data
        os.remove(start_thumb_path)
        os.remove(mid_thumb_path)
        os.remove(end_thumb_path)
        return thumbnail_path

    def generate_video_for_web(self, file_full_path):
        """Generates a web friendly version for the given video.

        :param str file_full_path: A string showing the full path of the video
          file.
        """
        web_version_full_path = tempfile.mktemp(suffix=self.web_video_format)
        self.convert_to_webm(file_full_path, web_version_full_path)
        return web_version_full_path

    def generate_thumbnail(self, file_full_path):
        """Generates a thumbnail for the given link

        :param file_full_path: Generates a thumbnail for the given file in the
          given path
        :return str: returns the thumbnail path
        """
        extension = os.path.splitext(file_full_path)[-1]
        # check if it is an image or video or non of them
        if extension in self.image_formats:
            # generate a thumbnail from image
            return self.generate_image_thumbnail(file_full_path)
        elif extension in self.video_formats:
            return self.generate_video_thumbnail(file_full_path)

        # not an image nor a video so no thumbnail, raise RuntimeError
        raise RuntimeError('%s is not an image nor a video file, can not '
                           'generate a thumbnail for it!' %
                           file_full_path)

    def generate_media_for_web(self, file_full_path):
        """Generates a media suitable for web browsers.

        It will generate PNG for images, and a WebM for video files.

        :param file_full_path: Generates a web suitable version for the given
          file in the given path.
        :return str: returns the media file path.
        """
        extension = os.path.splitext(file_full_path)[-1]
        # check if it is an image or video or non of them
        if extension in self.image_formats:
            # generate a thumbnail from image
            return self.generate_image_for_web(file_full_path)
        elif extension in self.video_formats:
            return self.generate_video_for_web(file_full_path)

        # not an image nor a video so no thumbnail, raise RuntimeError
        raise RuntimeError('%s is not an image nor a video file!' %
                           file_full_path)

    @classmethod
    def generate_local_file_path(cls, extension=''):
        """Generates file paths in server side storage.

        :param extension: Desired file extension
        :return:
        """
        # upload it to the stalker server side storage path
        new_filename = uuid.uuid4().hex + extension
        first_folder = new_filename[:2]
        second_folder = new_filename[2:4]

        file_path = os.path.join(
            defaults.server_side_storage_path,
            first_folder,
            second_folder
        )

        file_full_path = os.path.join(
            file_path,
            new_filename
        )

        return file_full_path

    @classmethod
    def convert_file_link_to_full_path(cls, link_path):
        """OBSOLETE: converts the given Stalker Pyramid Local file link to a
        real full path.

        :param link_path: A link to a file in SPL starting with SPL
          (ex: SPL/b0/e6/b0e64b16c6bd4857a91be47fb2517b53.jpg)
        :returns: str
        """
        if not isinstance(link_path, (str, unicode)):
            raise TypeError(
                '"link_path" argument in '
                '%(class)s.convert_file_link_to_full_path() method should be '
                'a str, not %(link_path_class)s' % {
                    'class': cls.__name__,
                    'link_path_class': link_path.__class__.__name__
                }
            )

        if not link_path.startswith('SPL'):
            raise ValueError(
                '"link_path" argument in '
                '%(class)s.convert_file_link_to_full_path() method should be '
                'a str starting with "SPL/"' % {
                    'class': cls.__name__
                }
            )

        spl_prefix = 'SPL/'
        if spl_prefix in link_path:
            link_full_path = link_path[len(spl_prefix):]
        else:
            link_full_path = link_path

        file_full_path = os.path.join(
            defaults.server_side_storage_path,
            link_full_path
        )
        return file_full_path

    @classmethod
    def convert_full_path_to_file_link(cls, full_path):
        """OBSOLETE: Converts the given full path to Stalker Pyramid Local
        Storage relative path.

        :param full_path: The full path of the file in SPL.
          (ex: /home/stalker/Stalker_Storage/b0/e6/b0e64b16c6bd4857a91be47fb2517b53.jpg)
        :returns: str
        """
        if not isinstance(full_path, (str, unicode)):
            raise TypeError(
                '"full_path" argument in '
                '%(class)s.convert_full_path_to_file_link() method should be '
                'a str, not %(full_path_class)s' % {
                    'class': cls.__name__,
                    'full_path_class': full_path.__class__.__name__
                }
            )

        if not full_path.startswith(defaults.server_side_storage_path):
            raise ValueError(
                '"full_path" argument in '
                '%(class)s.convert_full_path_to_file_link() method should be '
                'a str starting with "%(spl)s"' % {
                    'class': cls.__name__,
                    'spl': defaults.server_side_storage_path,
                }
            )

        spl_prefix = 'SPL/'
        return os.path.normpath(
            full_path.replace(defaults.server_side_storage_path, spl_prefix)
        )

    def get_video_info(self, full_path):
        """Returns the video info like the duration  in seconds and fps.

        Uses ffmpeg to extract information about the video file.

        :param str full_path: The full path of the video file
        :return: int
        """
        output_buffer = self.ffprobe(**{
            'show_streams': full_path,
        })

        video_info = []
        stream_info = {}

        print output_buffer

        import copy

        line = output_buffer.pop(0).strip()
        while line is not None:
            if line == '[STREAM]':
                # pop until you find [/STREAM]
                while line != '[/STREAM]':
                    print line
                    if '=' in line:
                        flag, value = line.split('=')
                        stream_info[flag] = value
                    line = output_buffer.pop(0).strip()

                copy_stream = copy.deepcopy(stream_info)
                video_info.append(copy_stream)
                stream_info = {}

            try:
                line = output_buffer.pop(0).strip()
            except IndexError:
                line = None

        print video_info
        return video_info

    def ffmpeg(self, **kwargs):
        """A simple python wrapper for ``ffmpeg`` command.
        """
        # there is only one special keyword called 'o'

        # this will raise KeyError if there is no 'o' key which is good to
        # prevent the rest to execute
        output = kwargs.get('o')
        try:
            kwargs.pop('o')
        except KeyError:  # no output
            pass

        # generate args
        args = [self.ffmpeg_command_path]
        for key in kwargs:
            flag = '-' + key
            value = kwargs[key]
            if not isinstance(value, list):
                # append the flag
                args.append(flag)
                # append the value
                args.append(str(value))
            else:
                # it is a multi flag
                # so append the flag every time you append the key
                for v in value:
                    args.append(flag)
                    args.append(str(v))

            # overwrite output

        # use all cpus
        import multiprocessing
        num_of_threads = multiprocessing.cpu_count()
        args.append('-threads')
        args.append('%s' % num_of_threads)

        # overwrite any file
        args.append('-y')

        # append the output
        if output != '' and output is not None:  # for info only
            args.append(output)

        logger.debug('calling ffmpeg with args: %s' % args)

        process = subprocess.Popen(args, stderr=subprocess.PIPE)

        # loop until process finishes and capture stderr output
        stderr_buffer = []
        while True:
            stderr = process.stderr.readline()

            if stderr == '' and process.poll() is not None:
                break

            if stderr != '':
                stderr_buffer.append(stderr)

        # if process.returncode:
        #     # there is an error
        #     raise RuntimeError(stderr_buffer)

        logger.debug(stderr_buffer)
        logger.debug('process completed!')
        return stderr_buffer

    def ffprobe(self, **kwargs):
        """A simple python wrapper for ``ffprobe`` command.
        """
        # generate args
        args = [self.ffprobe_command_path]
        for key in kwargs:
            flag = '-' + key
            value = kwargs[key]
            if not isinstance(value, list):
                # append the flag
                args.append(flag)
                # append the value
                args.append(str(value))
            else:
                # it is a multi flag
                # so append the flag every time you append the key
                for v in value:
                    args.append(flag)
                    args.append(str(v))

        logger.debug('calling ffprobe with args: %s' % args)

        process = subprocess.Popen(args, stdout=subprocess.PIPE)

        # loop until process finishes and capture stderr output
        stdout_buffer = []
        while True:
            stdout = process.stdout.readline()

            if stdout == '' and process.poll() is not None:
                break

            if stdout != '':
                stdout_buffer.append(stdout)

        # if process.returncode:
        #     # there is an error
        #     raise RuntimeError(stderr_buffer)

        logger.debug(stdout_buffer)
        logger.debug('process completed!')
        return stdout_buffer

    @classmethod
    def convert_to_h264(cls, input_path, output_path, options=None):
        """converts the given input to h264
        """
        if options is None:
            options = {}

        # change the extension to mp4
        output_path = '%s%s' % (os.path.splitext(output_path)[0], '.mp4')

        conversion_options = {
            'i': input_path,
            'vcodec': 'libx264',
            'b:v': '4096k',
            'o': output_path
        }
        conversion_options.update(options)

        cls.ffmpeg(**conversion_options)

        return output_path

    def convert_to_webm(self, input_path, output_path, options=None):
        """Converts the given input to webm format

        :param input_path: A string of path, can have wild card characters
        :param output_path: The output path
        :param options: Extra options to pass to the ffmpeg command
        :return:
        """
        if options is None:
            options = {}

        # change the extension to webm
        output_path = '%s%s' % (os.path.splitext(output_path)[0], '.webm')

        conversion_options = {
            'i': input_path,
            'vcodec': 'libvpx',
            'b:v': '%sk' % self.web_video_bitrate,
            'o': output_path
        }
        conversion_options.update(options)

        self.ffmpeg(**conversion_options)

        return output_path

    @classmethod
    def convert_to_animated_gif(cls, input_path, output_path, options=None):
        """converts the given input to animated gif

        :param input_path: A string of path, can have wild card characters
        :param output_path: The output path
        :param options: Extra options to pass to the ffmpeg command
        :return:
        """
        if options is None:
            options = {}

        # change the extension to gif
        output_path = '%s%s' % (os.path.splitext(output_path)[0], '.gif')

        conversion_options = {
            'i': input_path,
            'o': output_path
        }
        conversion_options.update(options)

        cls.ffmpeg(**conversion_options)

        return output_path

    @classmethod
    def upload_with_request_params(cls, file_params):
        """upload objects with request params

        :param file_params: An object with two attributes, first a
          ``filename`` attribute and a ``file`` which is a file like object.
        """
        uploaded_file_info = []
        # get the file names
        for file_param in file_params:
            filename = file_param.filename
            file_object = file_param.file
            extension = os.path.splitext(filename)[1]

            # upload to a temp path
            uploaded_file_full_path = cls.upload_file(
                file_object,
                tempfile.mktemp(suffix=extension),
                filename
            )

            # return the file information
            file_info = {
                'full_path': uploaded_file_full_path,
                'original_filename': filename
            }

            uploaded_file_info.append(file_info)

        return uploaded_file_info

    def randomize_file_name(self, full_path):
        """randomizes the file name by adding a the first 4 characters of a
        UUID4 sequence to it.

        :param str full_path: The filename to be randomized
        :return: str
        """
        # get the filename
        path = os.path.dirname(full_path)
        filename = os.path.basename(full_path)

        # get the base name
        basename, extension = os.path.splitext(filename)

        # generate uuid4 sequence until there is no file with that name
        def generate():
            random_part = '_%s' % uuid.uuid4().hex[:4]
            return os.path.join(
                path, '%s%s%s' % (basename, random_part, extension)
            )

        random_file_full_path = generate()
        # generate until we have something unique
        # it will be the first one 99.9% of time
        while os.path.exists(random_file_full_path):
            random_file_full_path = generate()

        return random_file_full_path

    def upload_file(self, file_object, file_path=None, filename=None):
        """Uploads files to the given path.

        The data of the files uploaded from a Web application is hold in a file
        like object. This method dumps the content of this file like object to
        the given path.

        :param file_object: File like object holding the data.
        :param str file_path: The path of the file to output the data to. If it
          is skipped the data will be written to a temp folder.
        :param str filename: The desired file name for the uploaded file. If it
          is skipped a unique temp filename will be generated.
        """
        if file_path is None:
            file_path = tempfile.gettempdir()

        if filename is None:
            filename = tempfile.mktemp(dir=file_path)

        file_full_path = os.path.join(file_path, filename)
        if os.path.exists(file_full_path):
            file_full_path = self.randomize_file_name(file_full_path)

        # write down to a temp file first
        temp_file_full_path = '%s~' % file_full_path

        # create folders
        try:
            os.makedirs(file_path)
        except OSError:  # Path exist
            pass

        with open(temp_file_full_path, 'wb') as output_file:
            file_object.seek(0)
            while True:
                data = file_object.read(2 << 16)
                if not data:
                    break
                output_file.write(data)

        # data is written completely, rename temp file to original file
        os.rename(temp_file_full_path, file_full_path)

        return file_full_path

    def upload_reference(self, task, file_object, filename):
        """Uploads a reference for the given task to
        Task.path/References/Stalker_Pyramid/ folder and create a Link object
        to there. Again the Link object will have a Repository root relative
        path.

        It will also create a thumbnail under
        {{Task.absolute_path}}/References/Stalker_Pyramid/Thumbs folder and a
        web friendly version (PNG for images, WebM for video files) under
        {{Task.absolute_path}}/References/Stalker_Pyramid/ForWeb folder.

        :param task: The task that a reference is uploaded to. Should be an
          instance of :class:`.Task` class.
        :type task: :class:`.Task`
        :param file_object: The file like object holding the content of the
          uploaded file.
        :param str filename: The original filename.
        :returns: :class:`.Link` instance.
        """
        ############################################################
        # ORIGINAL
        ############################################################
        file_path = os.path.join(
            os.path.join(task.absolute_path),
            self.reference_path
        )

        # upload it
        reference_file_full_path = \
            self.upload_file(file_object, file_path, filename)

        reference_file_file_name = os.path.basename(reference_file_full_path)
        reference_file_base_name = \
            os.path.splitext(reference_file_file_name)[0]

        # create a Link instance and return it.
        # use a Repository relative path
        repo = task.project.repository
        assert isinstance(repo, Repository)
        relative_full_path = repo.make_relative(reference_file_full_path)

        link = Link(full_path=relative_full_path, original_filename=filename)

        # create a thumbnail for the given reference
        # don't forget that the first thumbnail is the Web viewable version
        # and the second thumbnail is the thumbnail

        ############################################################
        # WEB VERSION
        ############################################################
        web_version_temp_full_path = \
            self.generate_media_for_web(reference_file_full_path)
        web_version_extension = \
            os.path.splitext(web_version_temp_full_path)[-1]
        web_version_full_path = \
            os.path.join(
                os.path.dirname(reference_file_full_path),
                'ForWeb',
                reference_file_base_name + web_version_extension
            )
        web_version_repo_relative_full_path = \
            repo.make_relative(web_version_full_path)
        web_version_link = Link(
            full_path=web_version_repo_relative_full_path,
            original_filename=filename
        )

        # move it to repository
        try:
            os.makedirs(os.path.dirname(web_version_full_path))
        except OSError:  # path exists
            pass
        shutil.move(web_version_temp_full_path, web_version_full_path)

        ############################################################
        # THUMBNAIL
        ############################################################
        # finally generate a Thumbnail
        thumbnail_temp_full_path = \
            self.generate_thumbnail(reference_file_full_path)
        thumbnail_extension = os.path.splitext(thumbnail_temp_full_path)[-1]

        thumbnail_full_path = \
            os.path.join(
                os.path.dirname(reference_file_full_path),
                'Thumbnail',
                reference_file_base_name + thumbnail_extension
            )
        thumbnail_repo_relative_full_path = \
            repo.make_relative(thumbnail_full_path)
        thumbnail_link = Link(
            full_path=thumbnail_repo_relative_full_path,
            original_filename=filename
        )

        # move it to repository
        try:
            os.makedirs(os.path.dirname(thumbnail_full_path))
        except OSError:  # path exists
            pass
        shutil.move(thumbnail_temp_full_path, thumbnail_full_path)

        ############################################################
        # LINK Objects
        ############################################################
        # link them
        # assign it as a reference to the given task
        task.references.append(link)
        link.thumbnail = web_version_link
        web_version_link.thumbnail = thumbnail_link

        return link

    def upload_version(self, task, file_object, take_name=None, extension=''):
        """Uploads versions to the Task.path/ folder and creates a Version
        object to there. Again the Version object will have a Repository root
        relative path.

        The filename of the version will be automatically generated by Stalker.

        :param task: The task that a version is uploaded to. Should be an
          instance of :class:`.Task` class.
        :param file_object: A file like object holding the content of the
          version.
        :param str take_name: A string showing the the take name of the
          Version. If skipped defaults.version_take_name will be used.
        :param str extension: The file extension of the version.
        :returns: :class:`.Version` instance.
        """
        if take_name is None:
            take_name = defaults.version_take_name

        v = Version(task=task,
                    take_name=take_name,
                    created_with='Stalker Pyramid')
        v.update_paths()
        v.extension = extension

        # upload it
        self.upload_file(file_object, v.absolute_path, v.filename)

        return v

    def upload_version_output(self, version, file_object, filename):
        """Uploads a file as an output for the given :class:`.Version`
        instance. Will store the file in
        {{Version.absolute_path}}/Outputs/Stalker_Pyramid/ folder.

        It will also generate a thumbnail in
        {{Version.absolute_path}}/Outputs/Stalker_Pyramid/Thumbs folder and a
        web friendly version (PNG for images, WebM for video files) under
        {{Version.absolute_path}}/Outputs/Stalker_Pyramid/ForWeb folder.

        :param version: A :class:`.Version` instance that the output is
          uploaded for.
        :type version: :class:`.Version`
        :param file_object: The file like object holding the content of the
          uploaded file.
        :param str filename: The original filename.
        :returns: :class:`.Link` instance.
        """
        ############################################################
        # ORIGINAL
        ############################################################
        file_path = os.path.join(
            os.path.join(version.absolute_path),
            self.version_output_path
        )

        # upload it
        version_output_file_full_path = \
            self.upload_file(file_object, file_path, filename)

        version_output_file_name = \
            os.path.basename(version_output_file_full_path)
        version_output_base_name = \
            os.path.splitext(version_output_file_name)[0]

        # create a Link instance and return it.
        # use a Repository relative path
        repo = version.task.project.repository
        assert isinstance(repo, Repository)
        relative_full_path = repo.make_relative(version_output_file_full_path)

        link = Link(full_path=relative_full_path, original_filename=filename)

        # create a thumbnail for the given version output
        # don't forget that the first thumbnail is the Web viewable version
        # and the second thumbnail is the thumbnail

        ############################################################
        # WEB VERSION
        ############################################################
        web_version_temp_full_path = \
            self.generate_media_for_web(version_output_file_full_path)
        web_version_extension = \
            os.path.splitext(web_version_temp_full_path)[-1]
        web_version_full_path = \
            os.path.join(
                os.path.dirname(version_output_file_full_path),
                'ForWeb',
                version_output_base_name + web_version_extension
            )
        web_version_repo_relative_full_path = \
            repo.make_relative(web_version_full_path)
        web_version_link = Link(
            full_path=web_version_repo_relative_full_path,
            original_filename=filename
        )

        # move it to repository
        try:
            os.makedirs(os.path.dirname(web_version_full_path))
        except OSError:  # path exists
            pass
        shutil.move(web_version_temp_full_path, web_version_full_path)

        ############################################################
        # THUMBNAIL
        ############################################################
        # finally generate a Thumbnail
        thumbnail_temp_full_path = \
            self.generate_thumbnail(version_output_file_full_path)
        thumbnail_extension = os.path.splitext(thumbnail_temp_full_path)[-1]

        thumbnail_full_path = \
            os.path.join(
                os.path.dirname(version_output_file_full_path),
                'Thumbnail',
                version_output_base_name + thumbnail_extension
            )
        thumbnail_repo_relative_full_path = \
            repo.make_relative(thumbnail_full_path)
        thumbnail_link = Link(
            full_path=thumbnail_repo_relative_full_path,
            original_filename=filename
        )

        # move it to repository
        try:
            os.makedirs(os.path.dirname(thumbnail_full_path))
        except OSError:  # path exists
            pass
        shutil.move(thumbnail_temp_full_path, thumbnail_full_path)

        ############################################################
        # LINK Objects
        ############################################################
        # link them
        # assign it as an output to the given version
        version.outputs.append(link)
        link.thumbnail = web_version_link
        web_version_link.thumbnail = thumbnail_link

        return link
