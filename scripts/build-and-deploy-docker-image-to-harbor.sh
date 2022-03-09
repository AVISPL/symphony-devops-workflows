#!/bin/bash
# usage:
# ./build-and-deploy-docker-image-to-harbor[-m <microservice>]

LOG_FILE="/symphony/build/log_$(date +%Y%m%d_%H%M%S).txt"

for ARG in "$@"
do
	if [ "${ARG}" = "-m" ]; then
		# next element is microservice name
		ARG_MICROSERVICE=true
	elif [ "$ARG_MICROSERVICE" = true ]; then
		MICROSERVICE="${ARG}"
		ARG_MICROSERVICE=false
	fi
done

eval "exec >&  >(tee -a ${LOG_FILE})"
FAILED_SUBJECT="${MICROSERVICE} Docker Image Creation Failed"
SUCCESS_SUBJECT="${MICROSERVICE} Docker Image Creation Succeeded"
NOT_A_MICROSERVICE="NOT_A_MICROSERVICE"
	
if [ -z "$MICROSERVICE" ]; then 
	echo "microservice argument cannot be null...Unable to build and tag docker image!";
elif [ "$MICROSERVICE" = "$NOT_A_MICROSERVICE" ]; then
	echo "Not a microservice, skipping docker tasks";
else 
	echo "Building and tagging Docker image for ${MICROSERVICE}";
	echo "Determining version of ${MICROSERVICE}"
	# determine version
	PROJECT_VERSION=$(mvn -B -q -f . -Dexec.executable="echo" -Dexec.args='${project.version}' --non-recursive exec:exec)
	if [ $PROJECT_VERSION == "\[ERROR\]*" ]; then
		echo "${MICROSERVICE} project version determination failed"
		exit 1
	fi
	
	# remove "SNAPSHOT" from PROJECT_VERSION and append current yymmddHHMM to it
	VERSION="${PROJECT_VERSION%SNAPSHOT}$(echo $(date +%y%m%d%H%M) | cut -c 1-11)"
	
	if grep -q -m 1 -e "\] Error" -e "\[Error\]" ${LOG_FILE}; then
		echo "${MICROSERVICE} version determination failed"
		mail -s "${FAILED_SUBJECT}" Development@avispl.com < ${LOG_FILE}
	else
		echo "building ${MICROSERVICE} v.${VERSION} docker image"
		sudo docker build -t ${MICROSERVICE}:${VERSION} .
		if grep -q -m 1 -e "\] Error" -e "\[Error\]" ${LOG_FILE}; then
			echo "building ${MICROSERVICE} v.${VERSION} docker image failed"
			mail -s "${FAILED_SUBJECT}" Development@avispl.com < ${LOG_FILE}
		else
			echo "tagging ${MICROSERVICE} v.${VERSION} docker image"
			sudo docker tag ${MICROSERVICE}:${VERSION} registry.vnocsymphony.com/symphony-microservices/${MICROSERVICE}:${VERSION}
			if grep -q -m 1 -e "\] Error" -e "\[Error\]" ${LOG_FILE}; then
				echo "tagging ${MICROSERVICE} v.${VERSION} docker image failed"
				mail -s "${FAILED_SUBJECT}" Development@avispl.com < ${LOG_FILE}
			else
				echo "pushing ${MICROSERVICE} v.${VERSION} docker tag to harbor registry"
				sudo docker push registry.vnocsymphony.com/symphony-microservices/${MICROSERVICE}:${VERSION}
				if grep -q -m 1 -e "\] Error" -e "\[Error\]" ${LOG_FILE}; then
					echo "pushing ${MICROSERVICE} v.${VERSION} docker tag to harbor registry failed"
					mail -s "${FAILED_SUBJECT}" Development@avispl.com < ${LOG_FILE}
				else
					echo "tagging ${MICROSERVICE} SNAPSHOT version docker image"
					sudo docker tag registry.vnocsymphony.com/symphony-microservices/${MICROSERVICE}:${VERSION} registry.vnocsymphony.com/symphony-microservices/${MICROSERVICE}:snapshot
					if grep -q -m 1 -e "\] Error" -e "\[Error\]" ${LOG_FILE}; then
						echo "tagging ${MICROSERVICE} SNAPSHOT version docker image" failed
						mail -s "${FAILED_SUBJECT}" Development@avispl.com < ${LOG_FILE}
					else
						echo "pushing ${MICROSERVICE} SNAPSHOT version docker image tag to harbor registry"
						sudo docker push registry.vnocsymphony.com/symphony-microservices/${MICROSERVICE}:snapshot
						if grep -q -m 1 -e "\] Error" -e "\[Error\]" ${LOG_FILE}; then
							echo "pushing ${MICROSERVICE} SNAPSHOT version docker image tag to harbor registry failed"
							mail -s "${FAILED_SUBJECT}" Development@avispl.com < ${LOG_FILE}
						else
							# Cleanup old images, if any. Following will locate and remove old ${MICROSERVICE} images, excluding one marked as snapshot, and ones marked with version this script is running under
							echo "locate and remove old ${MICROSERVICE} images, excluding one marked as 'snapshot', and ones marked with v.${VERSION} this script is running under"
							sudo docker rmi -f $(sudo docker images | grep -E "${MICROSERVICE}" | grep -v ${VERSION} | grep -v snapshot | awk '{printf"%s ",$3}')
							if grep -q -m 1 -e "\] Error" -e "\[Error\]" ${LOG_FILE}; then
								echo "Old ${MICROSERVICE} image(s) cleanup failed"
								mail -s "${FAILED_SUBJECT}" Development@avispl.com < ${LOG_FILE}
							else
								mail -s "${SUCCESS_SUBJECT}" Development@avispl.com < ${LOG_FILE}
							fi
						fi
					fi
				fi
			fi
		fi
	fi
fi
