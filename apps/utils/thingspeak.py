from celery import task
import traceback
import requests
import datetime
import string
import json
import time

from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, HttpResponse
from django.core import serializers
from django.core.cache import cache
from django.db.models import Q

from rest_framework.renderers import JSONRenderer

from apps.utils.models import (Channel, River,
                               Feed, Field,
                               AggregateMonthlyFeed,
                               AggregateDailyFeed,
                               ChannelField)

from apps.utils.api import (aggregateMonthlyFeedData,
                            aggregateDailyFeedData,
                            storeAggregatedData)

from apps.utils.serializers import (ChannelSerializer,
                                    FeedSerializer,
                                    RiverSerializer)


def getAPIData(url):
    r = requests.get(url)
    data = json.loads(r.text)
    if r.status_code == 200:
            return data
    else:
            return False


def getChannel():
    url = "http://api.thingspeak.com/channels/public.json?tag=mamase"

    data = getAPIData(url)

    if not data:
        return
    channels = data['channels']
    for item in channels:
        river = checkIfRiver(item['name'])

        #Check age of channel. If more than a day create. If not, skip
        if not checkOlderThanADay(item):
            continue

        if river:
            r, created = River.objects.get_or_create(name=river)
            river_id = r.id
            type_ = 'RIVER_DEPTH'
        else:
            river_id = None
            type_ = 'WEATHER_STATION'

        c, created = (Channel.objects
                      .get_or_create(data_id=item['id'],
                                     defaults={'elevation': item['elevation'],
                                               'description': item['description'],
                                               'name': item['name'],
                                               'longitude': item['longitude'],
                                               'latitude': item['latitude'],
                                               'created_at': item['created_at'],
                                               'last_entry_id': item['last_entry_id'],
                                               'river_id': river_id,
                                               'type': type_,
                                               }
                                     )
                      )


def parseAPIContent():
    getChannel()
    data_ids = Channel.objects.all().values_list('data_id', flat=True)
    for item in data_ids:
        getFeedData(str(item))

    return True


def getFeedData(data_id, start=None, results=None):
    url = "https://thingspeak.com/channels/"+str(data_id)+"/feed.json"
    if start:
        if not results:
            results = 8000
        url = "https://thingspeak.com/channels/"+str(data_id)+"/feed.json?results="+str(results)+"&start="+str(start)

    data = getAPIData(url)
    if not data:
        return
    ch = data['channel']
    feeds = data['feeds']
    channel = Channel.objects.get(data_id=data_id)
    #Gotta find more efficient way of doing this. Search for field elements.
    #Check size of returned list?
    #No solution from thingspeak. Gotta do a try and error.
    #At least do this when populating the channel data and just use the stored
    #data to pull feeds
    print "Now pulling data for channel " + str(channel.name)
    i = 1
    fields = []
    while i <= 8:
        field = "field" + str(i)
        f = ch.get(field)
        if f:
            f, created = Field.objects.get_or_create(name=f)
            c, created = ChannelField.objects.get_or_create(channel=channel,
                                                            field=f,
                                                            name=field)
            #c.save()
            fields.append(c)
        i += 1

    for item in feeds:
        for i in fields:
            if not checkIfWithinBound(item, i):
                continue
            try:
                if channel.type == "WEATHER_STATION":
                    f, created = Feed.objects.get_or_create(
                        entry_id=item['entry_id'],
                        channelfield=i,
                        defaults={'reading': checkIfFloat(item.get(i.name, None)),
                                  'sreading': item.get(i.name, None),
                                  'timestamp': item.get('created_at', None),
                                  }
                    )
                elif channel.type == "RIVER_DEPTH":
                    f, created = Feed.objects.get_or_create(
                        entry_id=item['entry_id'],
                        channelfield=i,
                        defaults={'sreading': item.get(i.name, None),
                                  'reading': checkIfFloat(item.get(i.name, None)),  # If float, save,
                                  'timestamp': item.get('created_at', None),
                                  }
                    )
                else:
                    print "Unrecognized channel type"
            except Exception, e:
                print "An error %s occured" % e
                traceback.print_exc()


class JSONResponse(HttpResponse):
    """
    An HttpResponse that renders its content into JSON.
    """
    def __init__(self, data, **kwargs):
        content = JSONRenderer().render(data)
        kwargs['content_type'] = 'application/json'
        super(JSONResponse, self).__init__(content, **kwargs)


@csrf_exempt
def returnChannelData(request):
    if request.method == 'GET':
        #cache_key = request.get_full_path()  # Use the full path as the cache key
        #result = cache.get(cache_key)
        fieldargs = {}

        #if result:
        #    return result

        type_ = request.GET.get('type', None)

        if type_ == 'WEATHER_STATION' or type_ == 'RIVER_DEPTH':
            channels = Channel.objects.filter(type=type_.upper())
        elif type_ == 'RAIN_TEMP':
            fieldargs = (Q(field__name__icontains='Temperature (C)') |
                         Q(field__name__icontains='Rain (mm'),)
            channels = Channel.objects.filter(type='WEATHER_STATION')
        else:
            channels = Channel.objects.all()

        rivers = River.objects.all()
        channelfields = []

        for i in channels:
            values = (i.channelfields.filter(*fieldargs)
                       .values('field__name', 'name',
                               'id', 'field__id').distinct()
                      )
            valuesdict = {'id': i.id, 'name': i.name,
                          'description': i.description,
                          'latitude': i.latitude,
                          'longitude': i.longitude,
                          'data_id': i.data_id,
                          'created_at': i.created_at,
                          'updated_at': i.updated_at,
                          'type': i.type,
                          'elevation': i.elevation,
                          'last_entry_id': i.last_entry_id}
            if i.river:
                valuesdict['river'] = i.river.id
            valuesdict['fields'] = list(values)
            channelfields.append(valuesdict)


        cserializer = ChannelSerializer(channels, many=True)
        rserializer = RiverSerializer(rivers, many=True)

        result = JSONResponse({'channels': channelfields,
                              'rivers': rserializer.data})

        #cache.set(cache_key, result)
        return result

    elif request.method == 'POST':
        data = JSONParser().parse(request)
        cserializer = ChannelSerializer(data=data)
        if cserializer.is_valid():
            cserializer.save()
            return JSONResponse(cserializer.data, status=201)
        return JSONResponse(cserializer.errors, status=400)


@csrf_exempt
def returnFeedData(request):
    if request.method == 'GET':
        feeds = Feed.objects.all()
        fserializer = FeedSerializer(feeds, many=True)
        return JSONResponse(fserializer.data)

    elif request.method == 'POST':
        data = JSONParser().parse(request)
        fserializer = FeedSerializer(data=data)
        if fserializer.is_valid():
            fserializer.save()
            return JSONResponse(fserializer.data, status=201)
        return JSONResponse(fserializer.errors, status=400)


def returnFeedDataOld(request):
    data = serializers.serialize("json", Feed.objects.all(),
                                 use_natural_foreign_keys=True,
                                 use_natural_primary_keys=True)
    return JsonResponse(dict(data=json.loads(data)))


def addClassicData(request):
    results = request.GET.get('records', None)
    start = request.GET.get('start', None)
    end = request.GET.get('end', None)

    if not results and not start:
        results = 8000
        start = ""

    ch = Channel.objects.all()

    for c in ch:
        getFeedData(c.data_id, start, results)
        storeAggregatedData(c.id, start, end)

    return HttpResponse("Done")


def clean(text):
    text = filter(lambda x: x in string.printable, text)
    text = text.split("=")[0]
    text = text.split("t/")[0]

    #So it seems some values are dates.
    #We might need to convert to milliseconds to store as float
    try:
        float(text)
    except:
        for fmt in ('%Y/%m/%d', '%Y:%m:%d', '%H:%M:%S'):
            try:
                #Assume this to be a date
                dt = datetime.datetime.strptime(text, fmt)
                text = time.mktime(dt.timetuple()) + (dt.microsecond / 1000000.0)
                return text
            except ValueError:
                pass
    return text


def checkIfWithinBound(item, channelfield):
    reading = checkIfFloat(item.get(channelfield.name, None))
    if reading:
        lower = channelfield.field.lower_bound
        upper = channelfield.field.upper_bound

        if lower is None or upper is None:
            return True

        if reading <= upper and reading >= lower:
            return True
        else:
            return False


def checkIfFloat(reading):
    try:
        reading = float(reading)
        return reading
    except Exception, e:
        return None


def checkIfRiver(name):
    namelist = name.split('@')
    if len(namelist) != 2:
        return None
    else:
        return namelist[0].strip()


def checkOlderThanADay(channel):
    created = channel['created_at']
    now = datetime.datetime.now()
    created_ts = datetime.datetime.strptime(created, '%Y-%m-%dT%H:%M:%SZ')

    diff = now - created_ts
    ts = diff.total_seconds()
    #How many days are these seconds? How many seconds are a day
    days_seconds = 86400
    if ts > days_seconds:  # If the time passed is more than a day
        return True
    else:
        return False