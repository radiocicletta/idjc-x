SUBDIRS = prelims

am__py_compile = $(top_srcdir)/py-compile

python_PYTHON = idjcmonitor.py

idjcpkgpython_PYTHON = dialogs.py gtkstuff.py irc.py jingles.py licence_window.py \
		maingui.py midicontrols.py mutagentagger.py songdb.py playergui.py \
		popupwindow.py preferences.py sourceclientgui.py tooltips.py utils.py \
		format.py

nodist_idjcpkgpython_PYTHON = __init__.py

dist_noinst_PYTHON = __init__.py.in.in


__init__.py: __init__.py.in
if GIT_VERSION_CONTROL
GIT_HEAD_COMMITHASH = $(shell git log -n1 "--pretty=format:%H")
GIT_HEAD_COMMITDATE = $(shell git log -n1 "--pretty=format:%ai")
GIT_REPO_STATUS = $(shell if test -z "$(git status --porcelain) 2>&1" ; then echo "clean" ; else echo "dirty" ; fi)
GIT_REMOTE_ORIGIN = $(shell git config --get remote.origin.url)
else
GIT_HEAD_COMMITHASH = none
GIT_HEAD_COMMITDATE = none
GIT_REPO_STATUS = none
GIT_REMOTE_ORIGIN = none
endif

	sed -e 's|$${pkgdatadir}|${pkgdatadir}|g'	\
		 -e 's|$${pkglibdir}|${pkglibdir}|g'	\
		 -e 's|$${exec_prefix}|${exec_prefix}|g' \
	     -e 's|$${prefix}|${prefix}|g'			\
	     -e 's|$${datarootdir}|${datarootdir}|g' \
	     -e 's|$${GIT_HEAD_COMMITHASH}|$(GIT_HEAD_COMMITHASH)|g' \
	     -e 's|$${GIT_HEAD_COMMITDATE}|$(GIT_HEAD_COMMITDATE)|g' \
	     -e 's|$${GIT_REPO_STATUS}|$(GIT_REPO_STATUS)|g' \
	     -e 's|$${GIT_REMOTE_ORIGIN}|$(GIT_REMOTE_ORIGIN)|g' \
	      $? >$@

clean-local:
	rm -f __init__.py

.PHONY: __init__.py $pkgpython_PYTHON
