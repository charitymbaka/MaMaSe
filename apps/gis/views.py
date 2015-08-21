from django.shortcuts import render_to_response, HttpResponseRedirect, render
from django.template.context import RequestContext
from django.core.urlresolvers import reverse
from django.views.generic import TemplateView

def index(request):    
    return render_to_response("gis/gis_platform.html", locals(), context_instance=RequestContext(request))
