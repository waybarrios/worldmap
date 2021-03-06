import json
import math
import urlparse
from httplib import HTTPConnection, HTTPSConnection
from urlparse import urlsplit

from django.conf import settings
from django.utils.http import is_safe_url
from django.http.request import validate_host
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse, HttpResponseRedirect, HttpResponseNotAllowed, HttpResponseServerError
from django.shortcuts import render_to_response, get_object_or_404, redirect
from django.template import RequestContext
from django.views.decorators.csrf import csrf_exempt

from geonode.base.models import TopicCategory
from geonode.geoserver.helpers import ogc_server_settings
from geonode.layers.models import Layer
from geonode.maps.models import Map, MapLayer
from geonode.utils import forward_mercator, default_map_config
from geonode.utils import llbbox_to_mercator
from geonode.layers.views import _resolve_layer
from geonode.maps.views import _resolve_map, _PERMISSION_MSG_VIEW
from geonode.maps.views import snapshot_config

from .models import LayerStats
from .forms import EndpointForm


@csrf_exempt
def proxy(request):
    PROXY_ALLOWED_HOSTS = getattr(settings, 'PROXY_ALLOWED_HOSTS', ())

    host = None

    if 'geonode.geoserver' in settings.INSTALLED_APPS:
        from geonode.geoserver.helpers import ogc_server_settings
        hostname = (ogc_server_settings.hostname,) if ogc_server_settings else ()
        PROXY_ALLOWED_HOSTS += hostname
        host = ogc_server_settings.netloc

    if 'url' not in request.GET:
        return HttpResponse("The proxy service requires a URL-encoded URL as a parameter.",
                            status=400,
                            content_type="text/plain"
                            )

    raw_url = request.GET['url']
    url = urlsplit(raw_url)
    locator = str(url.path)
    if url.query != "":
        locator += '?' + url.query
    if url.fragment != "":
        locator += '#' + url.fragment

    if not settings.DEBUG:
        if not validate_host(url.hostname, PROXY_ALLOWED_HOSTS):
            return HttpResponse("DEBUG is set to False but the host of the path provided to the proxy service"
                                " is not in the PROXY_ALLOWED_HOSTS setting.",
                                status=403,
                                content_type="text/plain"
                                )
    headers = {}

    if settings.SESSION_COOKIE_NAME in request.COOKIES and is_safe_url(url=raw_url, host=host):
        headers["Cookie"] = request.META["HTTP_COOKIE"]

    if request.method in ("POST", "PUT") and "CONTENT_TYPE" in request.META:
        headers["Content-Type"] = request.META["CONTENT_TYPE"]

    if url.scheme == 'https':
        conn = HTTPSConnection(url.hostname, url.port)
    else:
        conn = HTTPConnection(url.hostname, url.port)
    conn.request(request.method, locator, request.body, headers)

    result = conn.getresponse()

    # If we get a redirect, let's add a useful message.
    if result.status in (301, 302, 303, 307):
        response = HttpResponse(('This proxy does not support redirects. The server in "%s" '
                                 'asked for a redirect to "%s"' % (url, result.getheader('Location'))),
                                status=result.status,
                                content_type=result.getheader("Content-Type", "text/plain")
                                )

        response['Location'] = result.getheader('Location')
    else:
        response = HttpResponse(
            result.read(),
            status=result.status,
            content_type=result.getheader("Content-Type", "text/plain"))

    return response


def ajax_layer_edit_check(request, layername):
    """
    Check if the the layer style is editable.
    """
    # TODO implement this
    #layer = get_object_or_404(Layer, typename=layername);
    #editable = request.user.has_perm("maps.change_layer", obj=layer)
    editable = True
    return HttpResponse(
        str(editable),
        status=200 if editable else 403
    )

@login_required
def ajax_layer_update(request, layername):
    """
    Used to update layer bounds and gazetteer after an edit transaction.
    """
    # TODO implement this!
    return HttpResponse('')


@login_required
def create_pg_layer(request):
    # TODO implement this!
    #return redirect('layer_upload')
    return HttpResponse('')


@login_required
def upload_layer(request):
    # TODO implement this!
    return HttpResponse('')


def ajax_increment_layer_stats(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed('Only POST is supported')

    print request.POST
    if request.POST['layername'] != '':
        layer_match = Layer.objects.filter(typename=request.POST['layername'])[:1]
        for l in layer_match:
            layerStats,created = LayerStats.objects.get_or_create(layer=l)
            layerStats.visits += 1
            first_visit = True
            if request.session.get('visitlayer' + str(l.id), False):
                first_visit = False
            else:
                request.session['visitlayer' + str(l.id)] = True
            if first_visit or created:
                layerStats.uniques += 1
            layerStats.save()

    return HttpResponse(
        status=200
    )


def map_view_wm(request, mapid, snapshot=None, template='maps/map_view.html'):
    """
    The view that returns the map composer opened to
    the map with the given map ID.
    """
    map_obj = _resolve_map(request, mapid, 'base.view_resourcebase', _PERMISSION_MSG_VIEW)

    if 'access_token' in request.session:
        access_token = request.session['access_token']
    else:
        access_token = None

    if snapshot is None:
        config = map_obj.viewer_json(request.user, access_token)
    else:
        config = snapshot_config(snapshot, map_obj, request.user, access_token)

    geoexplorer2worldmap(config, map_obj)

    return render_to_response(template, RequestContext(request, {
        'config': json.dumps(config),
        'map': map_obj,
        'preview': getattr(
            settings,
            'LAYER_PREVIEW_LIBRARY',
            '')
    }))


def new_map_wm(request, template='maps/map_new.html'):
    config = new_map_config(request)
    context_dict = {
        'config': config,
        'USE_GAZETTEER': settings.USE_GAZETTEER
    }
    context_dict["preview"] = getattr(
        settings,
        'LAYER_PREVIEW_LIBRARY',
        '')
    if isinstance(config, HttpResponse):
        return config
    else:
        return render_to_response(template, RequestContext(request, context_dict))


def new_map_config(request):
    '''
    View that creates a new map.

    If the query argument 'copy' is given, the initial map is
    a copy of the map with the id specified, otherwise the
    default map configuration is used.  If copy is specified
    and the map specified does not exist a 404 is returned.
    '''

    # 3 cases in this function
    # 1. copy a map
    # 2. a new map with one or more layers
    # 3. a new map with no layers

    DEFAULT_MAP_CONFIG, DEFAULT_BASE_LAYERS = default_map_config(request)

    if 'access_token' in request.session:
        access_token = request.session['access_token']
    else:
        access_token = None

    # TODO support map copy
    if request.method == 'GET' and 'copy' in request.GET:
        mapid = request.GET['copy']
        map_obj = _resolve_map(request, mapid, 'base.view_resourcebase')

        map_obj.abstract = DEFAULT_ABSTRACT
        map_obj.title = DEFAULT_TITLE
        if request.user.is_authenticated():
            map_obj.owner = request.user

        config = map_obj.viewer_json(request.user, access_token)
        del config['id']
    else:
        if request.method == 'GET':
            params = request.GET
        elif request.method == 'POST':
            params = request.POST
        else:
            return HttpResponse(status=405)

        # a new map with a layer
        if 'layer' in params:
            bbox = None
            map_obj = Map(projection=getattr(settings, 'DEFAULT_MAP_CRS',
                          'EPSG:900913'))

            layers = []
            for layer_name in params.getlist('layer'):
                try:
                    layer = _resolve_layer(request, layer_name)
                except ObjectDoesNotExist:
                    # bad layer, skip
                    continue

                if not request.user.has_perm(
                        'view_resourcebase',
                        obj=layer.get_self_resource()):
                    # invisible layer, skip inclusion
                    continue

                layer_bbox = layer.bbox
                # assert False, str(layer_bbox)
                if bbox is None:
                    bbox = list(layer_bbox[0:4])
                else:
                    bbox[0] = min(bbox[0], layer_bbox[0])
                    bbox[1] = max(bbox[1], layer_bbox[1])
                    bbox[2] = min(bbox[2], layer_bbox[2])
                    bbox[3] = max(bbox[3], layer_bbox[3])

                config = layer.attribute_config()

                # Add required parameters for a WM layer
                config["local"] = True
                config["name"] = layer.alternate
                config["group"] = layer.category.identifier
                config["title"] = layer.title
                config["queryable"] = True
                config['tiled'] = True
                config['url'] = layer.ows_url
                config["srs"] = getattr(settings, 'DEFAULT_MAP_CRS', 'EPSG:900913')
                config["bbox"] = bbox if config["srs"] != 'EPSG:900913' \
                    else llbbox_to_mercator([float(coord) for coord in bbox])

                if layer.storeType == "remoteStore":
                    service = layer.service
                    # Probably not a good idea to send the access token to every remote service.
                    # This should never match, so no access token should be sent to remote services.
                    ogc_server_url = urlparse.urlsplit(ogc_server_settings.PUBLIC_LOCATION).netloc
                    service_url = urlparse.urlsplit(service.base_url).netloc

                    if access_token and ogc_server_url == service_url and 'access_token' not in service.base_url:
                        url = service.base_url+'?access_token='+access_token
                    else:
                        url = service.base_url
                    maplayer = MapLayer(map=map_obj,
                                        name=layer.typename,
                                        ows_url=layer.ows_url,
                                        layer_params=json.dumps(config),
                                        visibility=True,
                                        source_params=json.dumps({
                                            "ptype": service.ptype,
                                            "remote": True,
                                            "url": url,
                                            "name": service.name}))
                else:
                    ogc_server_url = urlparse.urlsplit(ogc_server_settings.PUBLIC_LOCATION).netloc
                    layer_url = urlparse.urlsplit(layer.ows_url).netloc

                    if access_token and ogc_server_url == layer_url and 'access_token' not in layer.ows_url:
                        url = layer.ows_url+'?access_token='+access_token
                    else:
                        url = layer.ows_url
                    maplayer = MapLayer(
                        map=map_obj,
                        name=layer.typename,
                        ows_url=url,
                        # use DjangoJSONEncoder to handle Decimal values
                        layer_params=json.dumps(config, cls=DjangoJSONEncoder),
                        visibility=True
                    )
                layers.append(maplayer)

            if bbox is not None:
                minx, miny, maxx, maxy = [float(coord) for coord in bbox]
                x = (minx + maxx) / 2
                y = (miny + maxy) / 2

                if getattr(settings, 'DEFAULT_MAP_CRS', 'EPSG:900913') == "EPSG:4326":
                    center = list((x, y))
                else:
                    center = list(forward_mercator((x, y)))

                if center[1] == float('-inf'):
                    center[1] = 0

                BBOX_DIFFERENCE_THRESHOLD = 1e-5

                # Check if the bbox is invalid
                valid_x = (maxx - minx) ** 2 > BBOX_DIFFERENCE_THRESHOLD
                valid_y = (maxy - miny) ** 2 > BBOX_DIFFERENCE_THRESHOLD

                if valid_x:
                    width_zoom = math.log(360 / abs(maxx - minx), 2)
                else:
                    width_zoom = 15

                if valid_y:
                    height_zoom = math.log(360 / abs(maxy - miny), 2)
                else:
                    height_zoom = 15

                map_obj.center_x = center[0]
                map_obj.center_y = center[1]
                map_obj.zoom = math.ceil(min(width_zoom, height_zoom))

            config = map_obj.viewer_json(
                request.user, access_token, *(DEFAULT_BASE_LAYERS + layers))

            config['fromLayer'] = True
            geoexplorer2worldmap(config, map_obj, layers)
        else:
            # a new map with no layer
            map_obj = Map(projection=getattr(settings, 'DEFAULT_MAP_CRS',
                          'EPSG:900913'))
            config = DEFAULT_MAP_CONFIG
            geoexplorer2worldmap(config, map_obj)

    return json.dumps(config)


def geoexplorer2worldmap(config, map_obj, layers=None):
    """
    Convert a GeoNode map config to the WorldMap client format.
    """
    topics = TopicCategory.objects.all()
    topicArray = []
    for topic in topics:
        topicArray.append([topic.identifier, topic.gn_description])
    topicArray.append(['General', 'General'])

    config['topic_categories'] = topicArray

    config['proxy'] = '/proxy/?url='

    # TODO check permissions here
    config['edit_map'] = True
    groups = set()

    # 2 cases: with new map (map_obj not saved) we use the layers parameter, otherwise
    # we get it from the map_obj
    if layers is None:
        layers = map_obj.layer_set.all()

    # 3 different layer types
    #
    # 1. background layer: group: background, ows_url: None
    #
    # 2. WM local layer:
    #    ows_url: http://localhost:8080/geoserver/wms,
    #    layer_params = {"selected": true, "title": "camer_hyd_basins_vm0_2007", "url": "http://localhost:8080/geoserver/wms",
    #       "tiled": true, "detail_url": "http://worldmap.harvard.edu/data/geonode:camer_hyd_basins_vm0_2007", "local": true,
    #       "llbbox": [-94.549682617, 9.553222656, -82.972412109, 18.762207031]}
    #
    # 3. WM remote layer (HH):
    #    ows_url: http://192.168.33.15:8002/registry/hypermap/layer/13ff2fea-d479-4fc7-87a6-3eab7d349def/map/wmts/market/default_grid/$%7Bz%7D/$%7Bx%7D/$%7By%7D.png
    #    layer_params = {"title": "market", "selected": true, "detail_url": "http://192.168.33.15:8002/registry/hypermap/layer/13ff2fea-d479-4fc7-87a6-3eab7d349def/", "local": false}

    for layer_config in config['map']['layers']:
        # detect if it is a WM or HH layer
        if 'local' in layer_config:
            # set the group
            group = layer_config['group']
            if group not in groups:
                groups.add(group)
            # TODO fix this accordingly to layer extent
            layer_config['llbbox'] = [-180,-90,180,90]
            # detect if it is a WM layer
            if layer_config['local'] == True:
                # WM local layer to process
                if 'styles' not in layer_config and Layer.objects.filter(alternate=layer_config['name']).exists():
                    layer = Layer.objects.get(alternate=layer_config['name'])
                    layer_config['styles'] = [style.name for style in layer.styles.all()]
                    #layer_config['styles'] = layer.default_style.name
            else:
                # detect if it is a HH layer
                layer_config['styles'] = ''
                # need to access to maplayer to read the url
	        ml = layers.filter(name=layer_config['name'])
                layer_config['url'] = ml[0].ows_url

    config['map']['groups'] = []
    for group in groups:
        if group not in json.dumps(config['map']['groups']):
            config['map']['groups'].append({"expanded":"true", "group":group})

    # TODO change intro text
    config['about']['introtext'] = 'Placeholder for intro text'


@login_required
def add_endpoint(request):
    """
    Let the user to add an endpoint for a remote service.
    """
    if request.method == 'POST':
        endpoint_form = EndpointForm(request.POST)
        if endpoint_form.is_valid():
            endpoint = endpoint_form.save(commit=False)
            endpoint.owner = request.user
            endpoint.save()
            return render_to_response(
                'wm_extra/endpoint_added.html',
                RequestContext(request, {
                    "endpoint": endpoint,
                })
            )
        else:
            logger.info('Error posting an endpoint')
    else:
        endpoint_form = EndpointForm()

    return render_to_response(
        'wm_extra/endpoint_add.html',
        RequestContext(request, {
            "form": endpoint_form,
        })
    )


def official_site(request, site):
    """
    The view that returns the map composer opened to
    the map with the given urlsuffix  site url.
    """
    map_obj = get_object_or_404(Map,urlsuffix=site)
    return map_view_wm(request, str(map_obj.id))
