import os
import uuid
from io import BytesIO

from pypdf import PdfWriter

import frappe
from frappe import _
from frappe.core.doctype.access_log.access_log import make_access_log
from frappe.translate import print_language
from frappe.utils.deprecations import deprecated
from frappe.utils.pdf import get_pdf

no_cache = 1

base_template_path = "www/printview.html"
standard_format = "templates/print_formats/standard.html"

from frappe.www.printview import validate_print_permission


@frappe.whitelist()
def download_multi_pdf(
	doctype,
	name,
	format=None,
	no_letterhead=False,
	letterhead=None,
	options=None,
):
	"""
	Calls _download_multi_pdf with the given parameters and returns a task ID
	"""
	task_id = str(uuid.uuid4())
	frappe.enqueue(
		_download_multi_pdf,
		doctype=doctype,
		name=name,
		format=format,
		no_letterhead=no_letterhead,
		letterhead=letterhead,
		options=options,
		task_id=task_id,
	)
	frappe.local.response["http_status_code"] = 201
	return {"task_id": task_id}


def _download_multi_pdf(
	doctype, name, format=None, no_letterhead=False, letterhead=None, options=None, task_id=None
):
	"""Return a PDF compiled by concatenating multiple documents.

	The documents can be from a single DocType or multiple DocTypes.

	Note: The design may seem a little weird, but it exists exists to
	        ensure backward compatibility. The correct way to use this function is to
	        pass a dict to doctype as described below

	NEW FUNCTIONALITY
	=================
	Parameters:
	doctype (dict):
	        key (string): DocType name
	        value (list): of strings of doc names which need to be concatenated and printed
	name (string):
	        name of the pdf which is generated
	format:
	        Print Format to be used

	OLD FUNCTIONALITY - soon to be deprecated
	=========================================
	Parameters:
	doctype (string):
	        name of the DocType to which the docs belong which need to be printed
	name (string or list):
	        If string the name of the doc which needs to be printed
	        If list the list of strings of doc names which needs to be printed
	format:
	        Print Format to be used

	Returns:
	PDF: A PDF generated by the concatenation of the mentioned input docs

	"""
	filename = ""

	import json

	pdf_writer = PdfWriter()

	if isinstance(options, str):
		options = json.loads(options)

	if not isinstance(doctype, dict):
		result = json.loads(name)
		total_docs = len(result)
		filename = f"{doctype}_"

		# Concatenating pdf files
		for idx, ss in enumerate(result):
			try:
				pdf_writer = frappe.get_print(
					doctype,
					ss,
					format,
					as_pdf=True,
					output=pdf_writer,
					no_letterhead=no_letterhead,
					letterhead=letterhead,
					pdf_options=options,
				)
			except Exception:
				frappe.publish_realtime(task_id=task_id, message={"message": "Failed"})

			# Publish progress
			frappe.publish_progress(
				percent=(idx + 1) / total_docs * 100,
				title=_("Please keep this tab open until the operation is complete."),
				description=_(f"Printed {idx + 1} documents out of {total_docs}."),
				task_id=task_id,
			)

	else:
		total_docs = sum([len(doctype[dt]) for dt in doctype])
		count = 1
		for doctype_name in doctype:
			filename += f"{doctype_name}_"
			for doc_name in doctype[doctype_name]:
				try:
					pdf_writer = frappe.get_print(
						doctype_name,
						doc_name,
						format,
						as_pdf=True,
						output=pdf_writer,
						no_letterhead=no_letterhead,
						letterhead=letterhead,
						pdf_options=options,
					)
				except Exception:
					frappe.publish_realtime(task_id=task_id, message="Failed")
					frappe.log_error(
						title="Error in Multi PDF download",
						message=f"Permission Error on doc {doc_name} of doctype {doctype_name}",
						reference_doctype=doctype_name,
						reference_name=doc_name,
					)
				count += 1
				frappe.publish_progress(
					percent=count / total_docs * 100,
					title=_("Please keep this tab open until the operation is complete."),
					description=_(f"Printed {count} documents out of {total_docs}."),
					task_id=task_id,
				)

	with BytesIO() as merged_pdf:
		pdf_writer.write(merged_pdf)
		_file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"{filename}{task_id}.pdf",
				"content": merged_pdf.getvalue(),
				"is_private": 1,
			}
		)
		_file.save()
		frappe.publish_realtime(f"task_progress:{task_id}", message={"file_url": _file.unique_url})


@deprecated
def read_multi_pdf(output: PdfWriter) -> bytes:
	with BytesIO() as merged_pdf:
		output.write(merged_pdf)
		return merged_pdf.getvalue()


@frappe.whitelist(allow_guest=True)
def download_pdf(doctype, name, format=None, doc=None, no_letterhead=0, language=None, letterhead=None):
	doc = doc or frappe.get_doc(doctype, name)
	validate_print_permission(doc)

	with print_language(language):
		pdf_file = frappe.get_print(
			doctype, name, format, doc=doc, as_pdf=True, letterhead=letterhead, no_letterhead=no_letterhead
		)

	frappe.local.response.filename = "{name}.pdf".format(name=name.replace(" ", "-").replace("/", "-"))
	frappe.local.response.filecontent = pdf_file
	frappe.local.response.type = "pdf"


@frappe.whitelist()
def report_to_pdf(html, orientation="Landscape"):
	make_access_log(file_type="PDF", method="PDF", page=html)
	frappe.local.response.filename = "report.pdf"
	frappe.local.response.filecontent = get_pdf(html, {"orientation": orientation})
	frappe.local.response.type = "pdf"


@frappe.whitelist()
def print_by_server(
	doctype, name, printer_setting, print_format=None, doc=None, no_letterhead=0, file_path=None
):
	print_settings = frappe.get_doc("Network Printer Settings", printer_setting)
	try:
		import cups
	except ImportError:
		frappe.throw(_("You need to install pycups to use this feature!"))

	try:
		cups.setServer(print_settings.server_ip)
		cups.setPort(print_settings.port)
		conn = cups.Connection()
		output = PdfWriter()
		output = frappe.get_print(
			doctype, name, print_format, doc=doc, no_letterhead=no_letterhead, as_pdf=True, output=output
		)
		if not file_path:
			file_path = os.path.join("/", "tmp", f"frappe-pdf-{frappe.generate_hash()}.pdf")
		output.write(open(file_path, "wb"))
		conn.printFile(print_settings.printer_name, file_path, name, {})
	except OSError as e:
		if (
			"ContentNotFoundError" in e.message
			or "ContentOperationNotPermittedError" in e.message
			or "UnknownContentError" in e.message
			or "RemoteHostClosedError" in e.message
		):
			frappe.throw(_("PDF generation failed"))
	except cups.IPPError:
		frappe.throw(_("Printing failed"))
